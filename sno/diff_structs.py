from collections import namedtuple


class Conflict(Exception):
    pass


class KeyValue(namedtuple("KeyValue", ("key", "value"))):
    """A key-value pair. A delta is made of two of these - one old, one new."""

    @staticmethod
    def of(obj):
        """Ensures that the given object is a KeyValue, or None."""
        if isinstance(obj, (KeyValue, type(None))):
            return obj
        elif isinstance(obj, tuple):
            return KeyValue(*obj)
        raise ValueError(f"Expected (key, value) tuple - got f{type(obj)}")


class Delta(namedtuple("Delta", ("old", "new"))):
    """
    An object changes from old to new. Either old or new can be None, for insert or delete operations.
    When present, old and new are both key-value pairs.
    The key identifies which object changed (so, should be a filename / address / primary key),
    and the value is the changed object's entire contents.
    If the old_key is different to the new_key, this means the object moved in this delta, ie a rename operation.
    Deltas can be concatenated together, if they refer to the same object - eg an delete + insert = update (usually).
    Deltas can be inverted, which just means old and new are swapped.
    """

    def __new__(cls, old, new):
        return super().__new__(cls, KeyValue.of(old), KeyValue.of(new))

    def __init__(self, old, new):
        super().__init__()
        if self.old is None and self.new is None:
            raise ValueError("Empty Delta")
        elif self.old is None:
            self.type = "insert"
        elif self.new is None:
            self.type = "delete"
        else:
            self.type = "update"

    @staticmethod
    def insert(new):
        return Delta(None, new)

    @staticmethod
    def update(old, new):
        return Delta(old, new)

    @staticmethod
    def maybe_update(old, new):
        return Delta(old, new) if old != new else None

    @staticmethod
    def delete(old):
        return Delta(old, None)

    def __invert__(self):
        return Delta(self.new, self.old)

    @property
    def old_key(self):
        return self.old.key if self.old is not None else None

    @property
    def new_key(self):
        return self.new.key if self.new is not None else None

    @property
    def key(self):
        # To be stored in a Diff, a Delta needs a single key.
        # This mostly works, but isn't perfect when renames are involved.
        return self.old_key or self.new_key

    def __add__(self, other):
        """Concatenate this delta with the subsequent delta, return the result as a single delta."""
        # Note: this method assumes that the deltas being concatenated are related,
        # ie that self.new == other.old. Don't try to concatenate arbitrary deltas together.

        if self.type == "insert":
            # ins + ins -> Conflict
            # ins + upd -> ins
            # ins + del -> noop
            if other.type == "insert":
                raise Conflict()
            elif other.type == "update":
                return Delta.insert(other.new)
            elif other.type == "delete":
                return None

        elif self.type == "update":
            # upd + ins -> Conflict
            # upd + upd -> upd?
            # upd + del -> del
            if other.type == "insert":
                raise Conflict()
            elif other.type == "update":
                return Delta.maybe_update(self.old, other.new)
            elif other.type == "delete":
                return Delta.delete(self.old)

        elif self.type == "delete":
            # del + ins -> upd?
            # del + del -> Conflict
            # del + upd -> Conflict
            if other.type == "insert":
                return Delta.maybe_update(self.old, other.new)
            else:
                raise Conflict()


class RichDict(dict):
    """
    A RichDict is a dict with some extra features, mostly useful when dealing with nested dicts with a
    well-defined structure.  It enforces that each node has children of the expected type. Using this
    type information it also supports getting or setting items deep in the nested tree using recursive_get
    or recursive_set, even if this involves creating extra dicts to contain the new value.
    """

    child_type = None

    def __init__(self, *args, **kwargs):
        if type(self) == RichDict:
            raise ValueError("RichDict is abstract - use a concrete subclass")
        super().__init__(*args, **kwargs)

    def ensure_child_type(self, key, value):
        if type(value) != self.child_type:
            raise TypeError(
                f"{type(self).__name__} accepts children of type {self.child_type.__name__} "
                f"but received {type(value).__name__}"
            )

    def __setitem__(self, key, value):
        self.ensure_child_type(key, value)
        super().__setitem__(key, value)

    def copy(self):
        return self.__class__(self)

    def empty_copy(self):
        return self.__class__()

    def __eq__(self, other):
        if type(self) != type(other):
            return False
        return super().__eq__(other)

    def __str__(self):
        # RichDicts are often deeply nested, so just show the keys for brevity.
        name = self.__class__.__name__
        return f"{name}(keys={{{','.join(repr(k) for k in self.keys())}}}))"

    __repr__ = __str__

    def recursive_get(self, keys, default=None):
        """Given a list of keys ["a", "b", "c"] returns self["a"]["b"]["c"] if it exists, or default."""
        if len(keys) == 0:
            raise ValueError("No keys")
        elif len(keys) == 1:
            return self.get(keys[0], None)
        key, *keys = keys
        child = self.get(key)
        return self.child.recursive_get(keys) if child is not None else default

    def recursive_set(self, keys, value):
        """
        Given a list of keys ["a", "b", "c"], sets self["a"]["b"]["c"] to value, constructing children as necessary.
        """

        if len(keys) == 0:
            raise ValueError("No keys")
        elif len(keys) == 1:
            self[keys[0]] = value
            return
        key, *keys = keys
        child = self.get(key)
        if child is None:
            child = self.create_empty_child(key)
        child.recursive_set(keys, value)

    def recursive_in(self, keys):
        """Given a list of keys ["a", "b", "c"] returns whether self["a"]["b"]["c"] exists."""
        if len(keys) == 0:
            raise ValueError("No keys")
        elif len(keys) == 1:
            return keys[0] in self
        key, *keys = keys
        child = self.get(key)
        return child.recursive_in(keys) if child is not None else False

    def create_empty_child(self, key):
        child = self.child_type()
        self[key] = child
        return child

    def prune(self):
        """Recursively deletes any empty RichDicts that are children of self."""
        if not issubclass(self.child_type, RichDict):
            return
        items = list(self.items())
        for key, value in items:
            value.prune()
            if not value:
                del self[key]


class Diff(RichDict):
    """
    A Diff is either a dict with the form {key: Delta}, or a dict with the form {key: Diff} for nested diffs.
    This means a RepoDiff can contain zero or more DatasetDiffs, each of which might contain up to two DeltaDiffs
    (one for meta, one for feature), and these DeltaDiffs finally contain the individual Deltas.
    When two diffs are concatenated, all their children with matching keys are recursively concatenated.
    """

    def __invert__(self):
        result = self.empty_copy()
        for key, value in self.items():
            result[key] = ~value
        return result

    def __add__(self, other, result=None):
        """Concatenate this Diff to the subsequent Diff, by concatenating all children with matching keys."""

        # FIXME: this algorithm isn't perfect when renames are involved.

        if type(self) != type(other):
            raise TypeError(f"Diff type mismatch: {type(self)} != {type(other)}")

        if result is None:
            result = self.empty_copy()

        for key in self.keys() | other.keys():
            lhs = self.get(key)
            rhs = other.get(key)
            if lhs and rhs:
                both = lhs + rhs
                if both:
                    result[key] = both
                else:
                    result.pop(key, None)
            else:
                result[key] = lhs or rhs
        return result

    def __iadd__(self, other):
        self.__add__(other, result=self)
        return self

    def to_filter(self):
        return {k: v.to_filter() for k, v in self.items()}

    def type_counts(self):
        return {k: v.type_counts() for k, v in self.items()}


class DeltaDiff(Diff):
    """
    A DeltaDiff is the inner-most type of Diff, the one that actually contains Deltas.
    Since Deltas know the keys at which they should be stored, a DeltaDiff makes sure to store Deltas at these keys.
    """

    child_type = Delta

    def __init__(self, initial_contents=()):
        if isinstance(initial_contents, dict):
            super().__init__(initial_contents)
        else:
            super().__init__((delta.key, delta) for delta in initial_contents)

    def __setitem__(self, key, delta):
        if key != delta.key:
            raise ValueError("Delta must be added at the appropriate key")
        super().__setitem__(key, delta)

    def add_delta(self, delta):
        """Add the given delta at the appropriate key."""
        super().__setitem__(delta.key, delta)

    def __invert__(self):
        result = self.empty_copy()
        for key, delta in self.items():
            result.add_delta(~delta)
        return result

    def to_filter(self):
        result = set()
        for delta in self.values():
            if delta.old is not None:
                result.add(str(delta.old.key))
            if delta.new is not None:
                result.add(str(delta.new.key))
        return result

    def type_counts(self):
        result = {}
        for delta in self.values():
            delta_type = delta.type
            result.setdefault(delta_type, 0)
            result[delta_type] += 1
        # Pluralise type names:
        return {f"{delta_type}s": value for delta_type, value in result.items()}


class DatasetDiff(Diff):
    """A DatasetDiff contains up to two DeltaDiffs, at keys "meta" or "feature"."""

    child_type = DeltaDiff


class RepoDiff(Diff):
    """A RepoDiff contains zero or more DatasetDiffs (one for each dataset that has changes)."""

    child_type = DatasetDiff

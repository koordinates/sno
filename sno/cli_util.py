import json
import click
import jsonschema


def add_help_subcommand(group):
    @group.command(add_help_option=False, hidden=True)
    @click.argument("topic", default=None, required=False, nargs=1)
    @click.pass_context
    def help(ctx, topic, **kw):
        # https://www.burgundywall.com/post/having-click-help-subcommand
        if topic is None:
            print(ctx.parent.get_help())
        else:
            print(group.commands[topic].get_help(ctx))

    return group


class MutexOption(click.Option):
    """
    Mutually exclusive options
    Source: merge of solutions from https://github.com/pallets/click/issues/257

    Usage:
        @click.group()
        @click.option("--username", prompt=True, cls=MutexOption, exclusive_with=["token"])
        @click.option("--password", prompt=True, hide_input=True, cls=MutexOption, exclusive_with=["token"])
        @click.option("--token", cls=MutexOption, exclusive_with=["username","password"])
        def login(ctx=None, username:str=None, password:str=None, token:str=None) -> None:
            print("...do what you like with the params you got...")
    """

    def __init__(self, *args, **kwargs):
        self.exclusive_with: list = kwargs.pop("exclusive_with")

        assert self.exclusive_with, "'exclusive_with' parameter required"
        kwargs["help"] = (
            kwargs.get("help", "")
            + "\nOption is mutually exclusive with "
            + ", ".join(self.exclusive_with)
            + "."
        ).strip()
        super().__init__(*args, **kwargs)

    def handle_parse_result(self, ctx, opts, args):
        current_opt: bool = self.name in opts
        for other_name in self.exclusive_with:

            if other_name in opts:
                if current_opt:
                    other = [x for x in ctx.command.params if x.name == other_name][0]
                    if not other.value_is_missing(opts[other_name]):
                        raise click.UsageError(
                            f"Illegal usage: {self.get_error_hint(ctx)} "
                            f"is mutually exclusive with {other.get_error_hint(ctx)}."
                        )
                else:
                    self.prompt = None
        return super().handle_parse_result(ctx, opts, args)


class StringFromFile(click.types.StringParamType):
    """
    Like a regular string option, but if the string starts with '@',
    the string will actually be read from the filename that follows.

    The special value "-" is a synonym for "@-", i.e. read from stdin.

    Usage:
        --param=value
            --> "value"
        --param=@filename.txt
            --> "This is the contents of filename.txt"
        --param=-
            --> "This is the contents of stdin"
    """

    def __init__(self, **file_kwargs):
        self.file_kwargs = file_kwargs

    def convert(self, value, param, ctx):
        value = super().convert(value, param, ctx)
        return string_or_string_from_file(value, param, ctx, **self.file_kwargs)


def string_or_string_from_file(value, param, ctx, **file_kwargs):
    if value == "-" or value.startswith("@"):
        filetype = click.File(**file_kwargs)
        filename = value[1:] if value.startswith("@") else value
        fp = filetype.convert(filename, param, ctx)
        return fp.read()

    return value


class JsonFromFile(StringFromFile):
    name = "json"

    def __init__(self, schema=None, **file_kwargs):
        super().__init__(**file_kwargs)
        self.schema = schema

    def convert(self, value, param, ctx):
        value = super().convert(value, param, ctx)
        try:
            value = json.loads(value)
        except json.JSONDecodeError as e:
            self.fail(
                f"Invalid JSON: {e}",
                param,
                ctx,
            )
        if self.schema:
            try:
                jsonschema.validate(instance=value, schema=self.schema)
            except jsonschema.ValidationError as e:
                self.fail(str(e), param, ctx)
        return value


def call_and_exit_flag(*args, callback, is_eager=True, **kwargs):
    """
    Add an is_flag option that, when set, eagerly calls the given callback with only the context as a parameter.
    The process exits once the callback is finished.
    Usage:
    @call_and_exit_flag("--version", callback=print_version, help="Print the version number")
    """

    def actual_callback(ctx, param, value):
        if value and not ctx.resilient_parsing:
            callback(ctx)
            ctx.exit()

    return click.option(
        *args,
        is_flag=True,
        callback=actual_callback,
        expose_value=False,
        is_eager=is_eager,
        **kwargs,
    )

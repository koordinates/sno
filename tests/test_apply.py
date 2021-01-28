import json
from pathlib import Path

import pytest
from sno.exceptions import NO_TABLE, PATCH_DOES_NOT_APPLY
from sno.sqlalchemy import gpkg_engine
from sno.repo import SnoRepo


H = pytest.helpers.helpers()
patches = Path(__file__).parent / "data" / "patches"


@pytest.mark.parametrize("input", ["{}", "this isnt json"])
def test_apply_invalid_patch(input, data_archive_readonly, cli_runner):
    with data_archive_readonly("points"):
        r = cli_runner.invoke(["apply", "-"], input=input)
        assert r.exit_code == 1, r
        assert "Failed to parse JSON patch file" in r.stderr


def test_apply_empty_patch(data_archive_readonly, cli_runner):
    with data_archive_readonly("points"):
        r = cli_runner.invoke(["apply", patches / "points-empty.snopatch"])
        assert r.exit_code == 44, r
        assert "No changes to commit" in r.stderr


def test_apply_nonempty_patch_which_makes_no_changes(data_archive_readonly, cli_runner):
    with data_archive_readonly("points"):
        # Despite appearances, this patch is empty because it makes a change
        # which has no actual effect.
        r = cli_runner.invoke(["apply", patches / "points-empty-2.snopatch"])
        assert r.exit_code == 44, r
        assert "No changes to commit" in r.stderr


def test_apply_with_wrong_dataset_name(data_archive, cli_runner):
    patch_data = json.dumps(
        {
            "sno.diff/v1+hexwkb": {
                "wrong-name": {
                    "featureChanges": [],
                    "metaChanges": [],
                }
            }
        }
    )
    with data_archive("points"):
        r = cli_runner.invoke(["apply", "-"], input=patch_data)
        assert r.exit_code == NO_TABLE, r
        assert (
            "Patch contains changes for dataset 'wrong-name' which is not in this repository"
            in r.stderr
        )


def test_apply_twice(data_archive, cli_runner):
    patch_path = patches / "points-1U-1D-1I.snopatch"
    with data_archive("points"):
        r = cli_runner.invoke(["apply", str(patch_path)])
        assert r.exit_code == 0, r.stderr

        r = cli_runner.invoke(["apply", str(patch_path)])
        assert r.exit_code == PATCH_DOES_NOT_APPLY

        assert (
            "nz_pa_points_topo_150k: Trying to delete nonexistent feature: 1241"
            in r.stderr
        )
        assert (
            "nz_pa_points_topo_150k: Trying to create feature that already exists: 9999"
            in r.stderr
        )
        assert (
            "nz_pa_points_topo_150k: Trying to update already-changed feature: 1795"
            in r.stderr
        )
        assert "Patch does not apply" in r.stderr


def test_apply_with_no_working_copy(data_archive, cli_runner):
    patch_filename = "updates-only.snopatch"
    message = "Change the Coromandel"
    author = {"name": "Someone", "time": 1561040913, "offset": 60}
    with data_archive("points") as repo_dir:
        patch_path = patches / patch_filename
        r = cli_runner.invoke(["apply", patch_path])
        assert r.exit_code == 0, r.stderr

        repo = SnoRepo(repo_dir)
        commit = repo.head_commit

        # the author details all come from the patch, including timestamp
        assert commit.message == message
        assert commit.author.name == author["name"]
        assert commit.author.time == author["time"]
        assert commit.author.offset == author["offset"]

        # the committer timestamp doesn't come from the patch
        assert commit.committer.time > commit.author.time
        bits = r.stdout.split()
        assert bits[0] == "Commit"

        # Check that the `sno create-patch` output is the same as our original patch file had.
        r = cli_runner.invoke(["create-patch", "HEAD"])
        assert r.exit_code == 0, r.stderr
        patch = json.loads(r.stdout)
        original_patch = json.load(patch_path.open("r", encoding="utf-8"))

        assert patch["sno.patch/v1"] == original_patch["sno.patch/v1"]
        assert patch["sno.diff/v1+hexwkb"] == original_patch["sno.diff/v1+hexwkb"]


def test_apply_meta_changes(data_archive, cli_runner):
    patch_file = json.dumps(
        {
            "sno.diff/v1+hexwkb": {
                "nz_pa_points_topo_150k": {
                    "meta": {
                        "title": {
                            "-": "NZ Pa Points (Topo, 1:50k)",
                            "+": "new title:",
                        }
                    }
                },
            },
            "sno.patch/v1": {
                "authorEmail": "robert@example.com",
                "authorName": "Robert Coup",
                "authorTime": "2019-06-20T14:28:33Z",
                "authorTimeOffset": "+01:00",
                "message": "Change the title",
            },
        }
    )

    with data_archive("points"):
        r = cli_runner.invoke(
            ["apply", "-"],
            input=patch_file,
        )
        assert r.exit_code == 0, r.stderr

        # Check that the `sno create-patch` output is the same as our original patch file had.
        r = cli_runner.invoke(["create-patch", "HEAD"])
        assert r.exit_code == 0
        patch = json.loads(r.stdout)
        meta = patch["sno.diff/v1+hexwkb"]["nz_pa_points_topo_150k"]["meta"]
        assert meta == {"title": {"+": "new title:", "-": "NZ Pa Points (Topo, 1:50k)"}}


def test_apply_user_info(data_archive, cli_runner):
    patch_file = json.dumps(
        {
            "sno.diff/v1+hexwkb": {
                "nz_pa_points_topo_150k": {
                    "meta": {
                        "title": {
                            "+": "new title:",
                        }
                    }
                },
            },
            "sno.patch/v1": {
                "authorEmail": "craig@example.com",
                "authorName": "Craig de Stigter",
                "authorTime": "2019-06-20T14:28:33Z",
                "authorTimeOffset": "+12:00",
                "message": "Change the title",
            },
        }
    )
    with data_archive("points"):
        r = cli_runner.invoke(
            ["apply", "--allow-missing-old-values", "-"],
            input=patch_file,
        )
        assert r.exit_code == 0, r.stderr

        # Check that the change was actually applied
        r = cli_runner.invoke(["create-patch", "HEAD"])
        assert r.exit_code == 0
        patch = json.loads(r.stdout)
        header = patch["sno.patch/v1"]
        assert header["authorEmail"] == "craig@example.com"
        assert header["authorName"] == "Craig de Stigter"


def test_apply_onto_other_ref(data_working_copy, cli_runner):
    patch_file = json.dumps(
        {
            "sno.diff/v1+hexwkb": {
                "nz_pa_points_topo_150k": {
                    "meta": {
                        "title": {
                            "+": "new title:",
                        }
                    }
                },
            },
            "sno.patch/v1": {
                "authorEmail": "craig@example.com",
                "authorName": "Craig de Stigter",
                "authorTime": "2019-06-20T14:28:33Z",
                "authorTimeOffset": "+12:00",
                "message": "Change the title",
            },
        }
    )
    with data_working_copy("points"):
        # First create another branch.
        r = cli_runner.invoke(["branch", "otherbranch"])
        assert r.exit_code == 0, r.stderr

        r = cli_runner.invoke(
            ["apply", "--allow-missing-old-values", "--ref=otherbranch", "-"],
            input=patch_file,
        )
        assert r.exit_code == 0, r.stderr
        # "Commit <hash>". Doesn't contain a workingcopy update!
        assert len(r.stdout.strip().splitlines()) == 1

        # Check that the change was applied to otherbranch
        r = cli_runner.invoke(["create-patch", "otherbranch"])
        assert r.exit_code == 0
        patch = json.loads(r.stdout)
        assert patch["sno.patch/v1"]["message"] == "Change the title"

        # But not to HEAD
        r = cli_runner.invoke(["create-patch", "HEAD"])
        assert r.exit_code == 0
        patch = json.loads(r.stdout)
        assert (
            patch["sno.patch/v1"]["message"]
            == "Improve naming on Coromandel East coast"
        )


def test_apply_allow_missing_old_values(data_archive, cli_runner):
    patch_file = json.dumps(
        {
            "sno.diff/v1+hexwkb": {
                "nz_pa_points_topo_150k": {
                    "meta": {
                        "title": {
                            "+": "new title:",
                        }
                    }
                },
            },
            "sno.patch/v1": {
                "authorEmail": "robert@example.com",
                "authorName": "Robert Coup",
                "authorTime": "2019-06-20T14:28:33Z",
                "authorTimeOffset": "+01:00",
                "message": "Change the title",
            },
        }
    )
    with data_archive("points"):
        # We ordinarily can't apply this patch,
        # because the "-" object for the title is missing.
        r = cli_runner.invoke(
            ["apply", "-"],
            input=patch_file,
        )
        assert r.exit_code == PATCH_DOES_NOT_APPLY, r.stderr

        # But --allow-missing-old-values lets us do it.
        r = cli_runner.invoke(
            ["apply", "--allow-missing-old-values", "-"],
            input=patch_file,
        )
        assert r.exit_code == 0, r.stderr

        # Check that the change was actually applied
        r = cli_runner.invoke(["create-patch", "HEAD"])
        assert r.exit_code == 0
        patch = json.loads(r.stdout)
        meta = patch["sno.diff/v1+hexwkb"]["nz_pa_points_topo_150k"]["meta"]
        assert meta == {"title": {"+": "new title:", "-": "NZ Pa Points (Topo, 1:50k)"}}


def test_apply_create_dataset(data_archive, cli_runner):
    patch_path = patches / "polygons.snopatch"
    with data_archive("points"):
        r = cli_runner.invoke(["data", "ls", "-o", "json"])
        assert r.exit_code == 0, r.stderr
        assert json.loads(r.stdout)["sno.data.ls/v1"] == ["nz_pa_points_topo_150k"]

        r = cli_runner.invoke(["apply", patch_path])
        assert r.exit_code == 0, r.stderr

        r = cli_runner.invoke(["data", "ls", "-o", "json"])
        assert r.exit_code == 0, r.stderr
        assert json.loads(r.stdout)["sno.data.ls/v1"] == [
            "nz_pa_points_topo_150k",
            "nz_waca_adjustments",
        ]

        # Check that the `sno create-patch` output is the same as our original patch file had.
        r = cli_runner.invoke(["create-patch", "HEAD"])
        assert r.exit_code == 0
        patch = json.loads(r.stdout)

        original_patch = json.load(patch_path.open("r", encoding="utf-8"))
        a, b = "sno.diff/v1+hexwkb", "nz_waca_adjustments"
        assert patch[a][b] == original_patch[a][b]


def test_add_and_remove_xml_metadata(data_archive, cli_runner):
    xml_content = '<gmd:MD_Metadata xmlns:gco="http://www.isotc211.org/2005/gco" xmlns:gmd="http://www.isotc211.org/2005/gmd" xmlns:gml="http://www.opengis.net/gml" xmlns:gts="http://www.isotc211.org/2005/gts" xmlns:topo="http://www.linz.govt.nz/schemas/topo/data-dictionary" xmlns:xlink="http://www.w3.org/1999/xlink" xmlns="http://www.isotc211.org/2005/gmd"><gmd:fileIdentifier><gco:CharacterString>d4334879-ee07-7ea0-b4df-fceb78106e94</gco:CharacterString></gmd:fileIdentifier><gmd:language><gco:CharacterString>eng</gco:CharacterString></gmd:language><gmd:characterSet><gmd:MD_CharacterSetCode codeList="http://asdd.ga.gov.au/asdd/profileinfo/gmxCodelists.xml#MD_CharacterSetCode" codeListValue="utf8">utf8</gmd:MD_CharacterSetCode></gmd:characterSet><gmd:hierarchyLevel><gmd:MD_ScopeCode codeList="http://asdd.ga.gov.au/asdd/profileinfo/GAScopeCodeList.xml#MD_ScopeCode" codeListValue="dataset">dataset</gmd:MD_ScopeCode></gmd:hierarchyLevel><gmd:hierarchyLevelName><gco:CharacterString>dataset</gco:CharacterString></gmd:hierarchyLevelName><gmd:contact><gmd:CI_ResponsibleParty><gmd:individualName><gco:CharacterString>omit</gco:CharacterString></gmd:individualName><gmd:organisationName><gco:CharacterString>LINZ - Land Information New Zealand</gco:CharacterString></gmd:organisationName><gmd:positionName><gco:CharacterString>Chief Topographer</gco:CharacterString></gmd:positionName><gmd:contactInfo><gmd:CI_Contact><gmd:phone><gmd:CI_Telephone><gmd:voice><gco:CharacterString>04 4600110</gco:CharacterString></gmd:voice></gmd:CI_Telephone></gmd:phone><gmd:address><gmd:CI_Address><gmd:deliveryPoint><gco:CharacterString>155 The Terrace</gco:CharacterString></gmd:deliveryPoint><gmd:city><gco:CharacterString>Wellington</gco:CharacterString></gmd:city><gmd:postalCode><gco:CharacterString>6145</gco:CharacterString></gmd:postalCode><gmd:country><gco:CharacterString>New Zealand</gco:CharacterString></gmd:country><gmd:electronicMailAddress><gco:CharacterString>info@linz.govt.nz</gco:CharacterString></gmd:electronicMailAddress></gmd:CI_Address></gmd:address></gmd:CI_Contact></gmd:contactInfo><gmd:role><gmd:CI_RoleCode codeList="http://asdd.ga.gov.au/asdd/profileinfo/gmxCodelists.xml#CI_RoleCode" codeListValue="resourceProvider">resourceProvider</gmd:CI_RoleCode></gmd:role></gmd:CI_ResponsibleParty></gmd:contact><gmd:dateStamp><gco:Date>2018-05-14</gco:Date></gmd:dateStamp><gmd:metadataStandardName><gco:CharacterString>ANZLIC Metadata Profile: An Australian/New Zealand Profile of AS/NZS ISO 19115:2005, Geographic information - Metadata</gco:CharacterString></gmd:metadataStandardName><gmd:metadataStandardVersion><gco:CharacterString>1.1</gco:CharacterString></gmd:metadataStandardVersion><gmd:identificationInfo><gmd:MD_DataIdentification><gmd:citation><gmd:CI_Citation><gmd:title><gco:CharacterString>NZ Pa Points (Topo, 1:50k)</gco:CharacterString></gmd:title><gmd:date><gmd:CI_Date><gmd:date><gco:Date>2009-09</gco:Date></gmd:date><gmd:dateType><gmd:CI_DateTypeCode codeList="http://asdd.ga.gov.au/asdd/profileinfo/gmxCodelists.xml#CI_DateTypeCode" codeListValue="creation">creation</gmd:CI_DateTypeCode></gmd:dateType></gmd:CI_Date></gmd:date></gmd:CI_Citation></gmd:citation><gmd:abstract><gco:CharacterString>Defensive earthworks constructed by Maori at any time between the fifteenth and nineteenth centuries which were still visible as a topographical object at the time the first edition of the map was published.\n\nData Dictionary for pa_pnt: http://apps.linz.govt.nz/topo-data-dictionary/index.aspx?page=class-pa_pnt\n\n\nThis layer is a component of the Topo50 map series. The Topo50 map series provides topographic mapping for the New Zealand mainland, Chatham and New Zealand\'s offshore islands, at 1:50,000 scale.\n\nFurther information on Topo50: \nhttp://www.linz.govt.nz/topography/topo-maps/topo50</gco:CharacterString></gmd:abstract><gmd:purpose><gco:CharacterString>Topo50 is the official topographic map series used by New Zealand emergency services.\n\nWhen using Topo50 data, please be aware of the following:\n1.  Representation of a road or track does not necessarily indicate public right of access.  For access rights, maps and other information, contact the New Zealand Walking Access Commission - www.walkingaccess.govt.nz\n\n2.  The Department of Conservation and other agencies should be contacted for the latest information on tracks and back country huts. Closed tracks are defined as being no longer maintained or passable and should not be used.\n\n3.  Not all aerial wires, cableways and obstructions that could be hazardous to aircraft are held in the data.\n\n4.  Contours and spot elevations in forest and snow areas may be less accurate.\n\n5. Not all pipelines including both underground and above ground are held in the data or shown on the printed maps. For the latest information please contact the utility and infrastructure agencies.\n\n6. Permits may be required to visit some sensitive and special islands and areas.  Contact the Department of Conservation to see if you need to apply for a permit.</gco:CharacterString></gmd:purpose><gmd:status><gmd:MD_ProgressCode codeList="http://asdd.ga.gov.au/asdd/profileinfo/gmxCodelists.xml#MD_ProgressCode" codeListValue="onGoing">onGoing</gmd:MD_ProgressCode></gmd:status><gmd:pointOfContact><gmd:CI_ResponsibleParty><gmd:individualName><gco:CharacterString>Omit</gco:CharacterString></gmd:individualName><gmd:organisationName><gco:CharacterString>LINZ - Land Information New Zealand</gco:CharacterString></gmd:organisationName><gmd:positionName><gco:CharacterString>Technical Leader, National Topographic Office</gco:CharacterString></gmd:positionName><gmd:contactInfo><gmd:CI_Contact><gmd:phone><gmd:CI_Telephone><gmd:voice><gco:CharacterString>0800 665 463 or +64 4 460 0110</gco:CharacterString></gmd:voice><gmd:facsimile><gco:CharacterString>+64 4 472 2244</gco:CharacterString></gmd:facsimile></gmd:CI_Telephone></gmd:phone><gmd:address><gmd:CI_Address><gmd:deliveryPoint><gco:CharacterString>155 The Terrace</gco:CharacterString></gmd:deliveryPoint><gmd:city><gco:CharacterString>Wellington</gco:CharacterString></gmd:city><gmd:administrativeArea><gco:CharacterString/></gmd:administrativeArea><gmd:postalCode><gco:CharacterString>6145</gco:CharacterString></gmd:postalCode><gmd:country><gco:CharacterString>New Zealand</gco:CharacterString></gmd:country><gmd:electronicMailAddress><gco:CharacterString>info@linz.govt.nz</gco:CharacterString></gmd:electronicMailAddress></gmd:CI_Address></gmd:address></gmd:CI_Contact></gmd:contactInfo><gmd:role><gmd:CI_RoleCode codeList="http://asdd.ga.gov.au/asdd/profileinfo/gmxCodelists.xml#CI_RoleCode" codeListValue="pointOfContact">pointOfContact</gmd:CI_RoleCode></gmd:role></gmd:CI_ResponsibleParty></gmd:pointOfContact><gmd:resourceMaintenance><gmd:MD_MaintenanceInformation><gmd:maintenanceAndUpdateFrequency><gmd:MD_MaintenanceFrequencyCode codeList="http://asdd.ga.gov.au/asdd/profileinfo/gmxCodelists.xml#MD_MaintenanceFrequencyCode" codeListValue="quarterly">quarterly</gmd:MD_MaintenanceFrequencyCode></gmd:maintenanceAndUpdateFrequency></gmd:MD_MaintenanceInformation></gmd:resourceMaintenance><gmd:resourceFormat><gmd:MD_Format><gmd:name><gco:CharacterString>*.xml</gco:CharacterString></gmd:name><gmd:version><gco:CharacterString>Unknown</gco:CharacterString></gmd:version></gmd:MD_Format></gmd:resourceFormat><gmd:descriptiveKeywords><gmd:MD_Keywords><gmd:keyword><gco:CharacterString>New Zealand</gco:CharacterString></gmd:keyword><gmd:type><gmd:MD_KeywordTypeCode codeList="http://asdd.ga.gov.au/asdd/profileinfo/gmxCodelists.xml#MD_KeywordTypeCode" codeListValue="theme">theme</gmd:MD_KeywordTypeCode></gmd:type><gmd:thesaurusName><gmd:CI_Citation><gmd:title><gco:CharacterString>ANZLIC Jurisdictions</gco:CharacterString></gmd:title><gmd:date><gmd:CI_Date><gmd:date><gco:Date>2008-10-29</gco:Date></gmd:date><gmd:dateType><gmd:CI_DateTypeCode codeList="http://asdd.ga.gov.au/asdd/profileinfo/gmxCodelists.xml#CI_DateTypeCode" codeListValue="revision">revision</gmd:CI_DateTypeCode></gmd:dateType></gmd:CI_Date></gmd:date><gmd:edition><gco:CharacterString>Version 2.1</gco:CharacterString></gmd:edition><gmd:editionDate><gco:Date>2008-10-29</gco:Date></gmd:editionDate><gmd:identifier><gmd:MD_Identifier><gmd:code><gco:CharacterString>http://asdd.ga.gov.au/asdd/profileinfo/anzlic-jurisdic.xml#anzlic-jurisdic</gco:CharacterString></gmd:code></gmd:MD_Identifier></gmd:identifier><gmd:citedResponsibleParty><gmd:CI_ResponsibleParty><gmd:organisationName><gco:CharacterString>ANZLIC the Spatial Information Council</gco:CharacterString></gmd:organisationName><gmd:role><gmd:CI_RoleCode codeList="http://asdd.ga.gov.au/asdd/profileinfo/gmxCodelists.xml#CI_RoleCode" codeListValue="custodian">custodian</gmd:CI_RoleCode></gmd:role></gmd:CI_ResponsibleParty></gmd:citedResponsibleParty></gmd:CI_Citation></gmd:thesaurusName></gmd:MD_Keywords></gmd:descriptiveKeywords><gmd:resourceConstraints><gmd:MD_SecurityConstraints><gmd:classification><gmd:MD_ClassificationCode codeList="http://asdd.ga.gov.au/asdd/profileinfo/gmxCodelists.xml#MD_ClassificationCode" codeListValue="unclassified">unclassified</gmd:MD_ClassificationCode></gmd:classification></gmd:MD_SecurityConstraints></gmd:resourceConstraints><gmd:resourceConstraints><gmd:MD_LegalConstraints><gmd:useLimitation><gco:CharacterString>Copyright 2011 Crown copyright (c)\n\nLand Information New Zealand and the New Zealand Government.\n\nAll rights reserved</gco:CharacterString></gmd:useLimitation><gmd:useConstraints><gmd:MD_RestrictionCode codeList="http://asdd.ga.gov.au/asdd/profileinfo/gmxCodelists.xml#MD_RestrictionCode" codeListValue="copyright">copyright</gmd:MD_RestrictionCode></gmd:useConstraints></gmd:MD_LegalConstraints></gmd:resourceConstraints><gmd:resourceConstraints><gmd:MD_LegalConstraints><gmd:useLimitation><gco:CharacterString>Released by LINZ under Creative Commons Attribution 4.0 International (CC BY 4.0) with:\n\nFollowing Disclaimers:\n1.  Representation of a road or track does not necessarily indicate public right of access.  For access rights, maps and other information, contact the New Zealand Walking Access Commission - www.walkingaccess.govt.nz\n2.  The Department of Conservation and other agencies should be contacted for the latest information on tracks and back country huts. Closed tracks are defined as being no longer maintained or passable and should not be used.\n3.  Not all aerial wires, cableways and obstructions that could be hazardous to aircraft are held in the data.\n4.  Contours and spot elevations in forest and snow areas may be less accurate.\n5.  Not all pipelines including both underground and above ground are held in the data or shown on the printed maps. For the latest information please contact the utility and infrastructure agencies\n6.  Permits may be required to visit some sensitive and special islands and areas.  Contact the Department of Conservation to see if you need to apply for a permit.\n\nFollowing Attribution:\n"Sourced from the LINZ Data Service and licensed for reuse under CC BY 4.0"\nFor details see https://www.linz.govt.nz/data/licensing-and-using-data/attributing-linz-data</gco:CharacterString></gmd:useLimitation><gmd:useConstraints><gmd:MD_RestrictionCode codeList="http://asdd.ga.gov.au/asdd/profileinfo/gmxCodelists.xml#MD_RestrictionCode" codeListValue="license">license</gmd:MD_RestrictionCode></gmd:useConstraints></gmd:MD_LegalConstraints></gmd:resourceConstraints><gmd:spatialRepresentationType><gmd:MD_SpatialRepresentationTypeCode codeList="http://asdd.ga.gov.au/asdd/profileinfo/gmxCodelists.xml#MD_SpatialRepresentationTypeCode" codeListValue="vector">vector</gmd:MD_SpatialRepresentationTypeCode></gmd:spatialRepresentationType><gmd:spatialResolution><gmd:MD_Resolution><gmd:equivalentScale><gmd:MD_RepresentativeFraction><gmd:denominator><gco:Integer>50000</gco:Integer></gmd:denominator></gmd:MD_RepresentativeFraction></gmd:equivalentScale></gmd:MD_Resolution></gmd:spatialResolution><gmd:language><gco:CharacterString>eng</gco:CharacterString></gmd:language><gmd:characterSet><gmd:MD_CharacterSetCode codeList="http://asdd.ga.gov.au/asdd/profileinfo/gmxCodelists.xml#MD_CharacterSetCode" codeListValue="utf8">utf8</gmd:MD_CharacterSetCode></gmd:characterSet><gmd:topicCategory><gmd:MD_TopicCategoryCode>imageryBaseMapsEarthCover</gmd:MD_TopicCategoryCode></gmd:topicCategory><gmd:extent><gmd:EX_Extent><gmd:geographicElement><gmd:EX_GeographicBoundingBox><gmd:westBoundLongitude><gco:Decimal>170.616768417</gco:Decimal></gmd:westBoundLongitude><gmd:eastBoundLongitude><gco:Decimal>178.430232985</gco:Decimal></gmd:eastBoundLongitude><gmd:southBoundLatitude><gco:Decimal>-45.7347756072</gco:Decimal></gmd:southBoundLatitude><gmd:northBoundLatitude><gco:Decimal>-34.4060931671</gco:Decimal></gmd:northBoundLatitude></gmd:EX_GeographicBoundingBox></gmd:geographicElement></gmd:EX_Extent></gmd:extent></gmd:MD_DataIdentification></gmd:identificationInfo><gmd:distributionInfo><gmd:MD_Distribution><gmd:transferOptions><gmd:MD_DigitalTransferOptions><gmd:onLine><gmd:CI_OnlineResource><gmd:linkage><gmd:URL>https://data.linz.govt.nz/layer/50308-nz-pa-points-topo-150k/</gmd:URL></gmd:linkage></gmd:CI_OnlineResource></gmd:onLine></gmd:MD_DigitalTransferOptions></gmd:transferOptions></gmd:MD_Distribution></gmd:distributionInfo><gmd:dataQualityInfo><gmd:DQ_DataQuality><gmd:scope><gmd:DQ_Scope><gmd:level><gmd:MD_ScopeCode codeList="http://asdd.ga.gov.au/asdd/profileinfo/gmxCodelists.xml#MD_ScopeCode" codeListValue="dataset">dataset</gmd:MD_ScopeCode></gmd:level><gmd:levelDescription><gmd:MD_ScopeDescription><gmd:other><gco:CharacterString>dataset</gco:CharacterString></gmd:other></gmd:MD_ScopeDescription></gmd:levelDescription></gmd:DQ_Scope></gmd:scope><gmd:lineage><gmd:LI_Lineage><gmd:statement><gco:CharacterString>LINZ and our predecessors have been responsible for national topographic mapping in New Zealand for more than a hundred years.\n\nThe first digital data at 1:50,000 was created in the late 80\'s and early 90\'s by scanning the 1:50,000 maps that existed at the time (known as the NZMS 260 series, which replaced the imperial NZMS 1 series at 1inch to 1 mile)\n\nThe raw data was created by photogrammetrists who from 1974 to 1997 mapped the country from overlapping pairs of aerial photographs. Cartographers then took the data and added symbols and text, and created the colour separations needed to produce the printed maps.\n\nFrom 1994 to 2006 LINZ used orthophotos to update the map data.  Today the map data is updated primarily from aerial and satellite imagery, and data supplied from Department of Conservation, Transit NZ and others.\n\n\n\nLINZ releases regular updates of the Topo50 maps and data; for details refer\nhttp://www.linz.govt.nz/topography/topo-maps/topo50/update-history\n\nSome features are subject to change more than others. For example in any given map revision, it is likely that road data will undergo more change than, for example, fumeroles.  However, all data is examined during a full data revision.</gco:CharacterString></gmd:statement></gmd:LI_Lineage></gmd:lineage></gmd:DQ_DataQuality></gmd:dataQualityInfo><gmd:metadataConstraints><gmd:MD_SecurityConstraints><gmd:classification><gmd:MD_ClassificationCode codeList="http://asdd.ga.gov.au/asdd/profileinfo/gmxCodelists.xml#MD_ClassificationCode" codeListValue="unclassified">unclassified</gmd:MD_ClassificationCode></gmd:classification></gmd:MD_SecurityConstraints></gmd:metadataConstraints><gmd:metadataConstraints><gmd:MD_LegalConstraints><gmd:useLimitation><gco:CharacterString>Copyright 2011 Crown copyright (c)\n\nLand Information New Zealand and the New Zealand Government.\n\nAll rights reserved</gco:CharacterString></gmd:useLimitation><gmd:useConstraints><gmd:MD_RestrictionCode codeList="http://asdd.ga.gov.au/asdd/profileinfo/gmxCodelists.xml#MD_RestrictionCode" codeListValue="copyright">copyright</gmd:MD_RestrictionCode></gmd:useConstraints></gmd:MD_LegalConstraints></gmd:metadataConstraints><gmd:metadataConstraints><gmd:MD_LegalConstraints><gmd:useLimitation><gco:CharacterString>Released under Creative Commons Attribution 4.0 International</gco:CharacterString></gmd:useLimitation><gmd:useConstraints><gmd:MD_RestrictionCode codeList="http://asdd.ga.gov.au/asdd/profileinfo/gmxCodelists.xml#MD_RestrictionCode" codeListValue="license">license</gmd:MD_RestrictionCode></gmd:useConstraints></gmd:MD_LegalConstraints></gmd:metadataConstraints></gmd:MD_Metadata>'
    orig_patch = {
        "sno.diff/v1+hexwkb": {
            "nz_pa_points_topo_150k": {
                "meta": {
                    "metadata/dataset.json": {
                        "-": {
                            "http://www.isotc211.org/2005/gmd": {
                                "text/xml": xml_content
                            }
                        }
                    }
                }
            }
        },
        "sno.patch/v1": {
            "authorEmail": "robert@example.com",
            "authorName": "Robert Coup",
            "authorTime": "2019-06-20T14:28:33Z",
            "authorTimeOffset": "+01:00",
            "message": "Add some XML metadata",
        },
    }
    patch_file = json.dumps(orig_patch)
    with data_archive("points"):
        r = cli_runner.invoke(
            ["apply", "-"],
            input=patch_file,
        )
        assert r.exit_code == 0, r.stderr

        # Check that the `sno create-patch` output is the same as our original patch file had.
        r = cli_runner.invoke(["create-patch", "HEAD"])
        assert r.exit_code == 0
        patch = json.loads(r.stdout)
        assert (
            patch["sno.diff/v1+hexwkb"]["nz_pa_points_topo_150k"]["meta"]
            == orig_patch["sno.diff/v1+hexwkb"]["nz_pa_points_topo_150k"]["meta"]
        )

        # check we can add it again too
        m = orig_patch["sno.diff/v1+hexwkb"]["nz_pa_points_topo_150k"]["meta"][
            "metadata/dataset.json"
        ]
        m["+"] = m.pop("-")
        patch_file = json.dumps(orig_patch)
        r = cli_runner.invoke(
            ["apply", "-"],
            input=patch_file,
        )
        assert r.exit_code == 0, r.stderr


def test_apply_with_working_copy(
    data_working_copy,
    cli_runner,
):
    patch_filename = "updates-only.snopatch"
    message = "Change the Coromandel"
    author = {"name": "Someone", "time": 1561040913, "offset": 60}
    with data_working_copy("points") as (repo_dir, wc_path):
        patch_path = patches / patch_filename
        r = cli_runner.invoke(["apply", patch_path])
        assert r.exit_code == 0, r.stderr

        repo = SnoRepo(repo_dir)
        commit = repo.head_commit

        # the author details all come from the patch, including timestamp
        assert commit.message == message
        assert commit.author.name == author["name"]
        assert commit.author.time == author["time"]
        assert commit.author.offset == author["offset"]

        # the committer timestamp doesn't come from the patch
        assert commit.committer.time > commit.author.time
        bits = r.stdout.split()
        assert bits[0] == "Commit"
        assert bits[2] == "Updating"

        with gpkg_engine(wc_path).connect() as db:
            r = db.execute(
                f"""
                SELECT name FROM {H.POINTS.LAYER} WHERE {H.POINTS.LAYER_PK} = 1095;
                """
            )
            assert r.scalar() is None

        # Check that the `sno create-patch` output is the same as our original patch file had.
        r = cli_runner.invoke(["create-patch", "HEAD"])
        assert r.exit_code == 0, r.stderr
        patch = json.loads(r.stdout)
        original_patch = json.load(patch_path.open("r", encoding="utf-8"))

        assert patch["sno.patch/v1"] == original_patch["sno.patch/v1"]
        assert patch["sno.diff/v1+hexwkb"] == original_patch["sno.diff/v1+hexwkb"]


def test_apply_with_no_working_copy_with_no_commit(data_archive_readonly, cli_runner):
    with data_archive_readonly("points"):
        r = cli_runner.invoke(
            ["apply", "--no-commit", patches / "updates-only.snopatch"]
        )
        assert r.exit_code == 45
        assert "--no-commit requires a working copy" in r.stderr


def test_apply_with_working_copy_with_no_commit(data_working_copy, cli_runner):
    patch_filename = "updates-only.snopatch"
    message = "Change the Coromandel"
    with data_working_copy("points") as (repo_dir, wc_path):
        patch_path = patches / patch_filename
        r = cli_runner.invoke(["apply", "--no-commit", patch_path])
        assert r.exit_code == 0, r.stderr

        repo = SnoRepo(repo_dir)

        # no commit was made
        commit = repo.head_commit
        assert commit.message != message

        bits = r.stdout.split()
        assert bits[0] == "Updating"

        # Check that the working copy diff is the same as the original patch file
        r = cli_runner.invoke(["diff", "-o", "json"])
        assert r.exit_code == 0
        patch = json.loads(r.stdout)
        original_patch = json.load(patch_path.open("r", encoding="utf-8"))

        assert patch["sno.diff/v1+hexwkb"] == original_patch["sno.diff/v1+hexwkb"]


def test_apply_multiple_dataset_patch_roundtrip(data_archive, cli_runner):
    with data_archive("au-census"):
        r = cli_runner.invoke(["create-patch", "master"])
        assert r.exit_code == 0, r.stderr
        patch_text = r.stdout
        patch_json = json.loads(patch_text)
        assert set(patch_json["sno.diff/v1+hexwkb"].keys()) == {
            "census2016_sdhca_ot_ra_short",
            "census2016_sdhca_ot_sos_short",
        }

        # note: repo's current branch is 'branch1' which doesn't have the commit on it,
        # so the patch applies cleanly.
        r = cli_runner.invoke(["apply", "-"], input=patch_text)
        assert r.exit_code == 0, r.stderr

        r = cli_runner.invoke(["create-patch", "HEAD"])
        assert r.exit_code == 0, r.stderr
        new_patch_json = json.loads(r.stdout)

        assert new_patch_json == patch_json


@pytest.mark.slow
def test_apply_benchmark(data_working_copy, benchmark, cli_runner, monkeypatch):
    from sno import apply

    with data_working_copy("points") as (repo_dir, wc_path):
        # Create a branch we can use later; don't switch to it
        r = cli_runner.invoke(["branch", "-c", "savepoint"])
        assert r.exit_code == 0, r.stderr

        # Generate a large change and commit it
        with gpkg_engine(wc_path).connect() as db:
            db.execute(
                "UPDATE nz_pa_points_topo_150k SET name = 'bulk_' || Coalesce(name, 'null')"
            )

        r = cli_runner.invoke(["commit", "-m", "rename everything"])
        assert r.exit_code == 0, r.stderr

        # Make it into a patch
        r = cli_runner.invoke(["create-patch", "HEAD"])
        assert r.exit_code == 0, r.stderr
        patch_text = r.stdout
        patch_json = json.loads(patch_text)
        assert patch_json["sno.patch/v1"]["message"] == "rename everything"

        # Now switch to our savepoint branch and apply the patch
        r = cli_runner.invoke(["checkout", "savepoint"])
        assert r.exit_code == 0, r.stderr

        # wrap the apply command with benchmarking
        orig_apply_patch = apply.apply_patch

        def _benchmark_apply(*args, **kwargs):
            # one round/iteration isn't very statistical, but hopefully crude idea
            return benchmark.pedantic(
                orig_apply_patch, args=args, kwargs=kwargs, rounds=1, iterations=1
            )

        monkeypatch.setattr(apply, "apply_patch", _benchmark_apply)

        cli_runner.invoke(["apply", "-"], input=patch_text)

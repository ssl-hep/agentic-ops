from app import ewkit


def test_parse_mc_dataset() -> None:
    d = ewkit.parse_dataset(
        "mc23_13p6TeV.700786.Sh_2214_Zee_maxHTpTV2_BFilter.deriv.DAOD_PHYS.e8514_s4162_r15540_p7266"
    )
    assert d.kind == "mc"
    assert d.dsid == "700786"
    assert d.physics_short == "Sh_2214_Zee_maxHTpTV2_BFilter"
    assert (d.etag, d.simtag, d.rtags, d.ptag) == ("e8514", "s4162", ("r15540",), "p7266")
    assert d.campaign == "mc23a"
    assert d.problem is None


def test_parse_mc_dataset_with_scope_and_merge_rtag() -> None:
    d = ewkit.parse_dataset("mc20_13TeV:mc20_13TeV.410470.PhPy8EG.deriv.DAOD_PHYS.e6337_s3681_r13144_r13146_p6266/")
    assert d.name.startswith("mc20_13TeV.410470")
    assert d.campaign == "mc20d"


def test_unknown_rtag_is_flagged() -> None:
    d = ewkit.parse_dataset("mc23_13p6TeV.700786.Sh.deriv.DAOD_PHYS.e8514_s4162_r99999_p7266")
    assert d.campaign == ewkit.UNKNOWN_CAMPAIGN
    assert d.problem == "Unknown r-tag r99999"


def test_parse_data_dataset() -> None:
    d = ewkit.parse_dataset("data15_13TeV.periodAllYear.physics_Main.PhysCont.DAOD_PHYS.grp15_v01_p7266")
    assert d.kind == "data"
    assert d.campaign == "data15"
    assert d.ptag == "p7266"


def test_unparseable_line() -> None:
    d = ewkit.parse_dataset("not-a-dataset")
    assert d.kind == "unknown"
    assert d.problem


def test_parse_sample_list_groups() -> None:
    text = """# Vjets: Z+jets
mc23_13p6TeV.700786.Sh.deriv.DAOD_PHYS.e8514_s4162_r15540_p7266

mc23_13p6TeV.700786.Sh.deriv.DAOD_PHYS.e8514_s4159_r15224_p7266
#
# ttbar
mc23_13p6TeV.601229.PhPy8EG.deriv.DAOD_PHYS.e8514_s4162_r16083_p7266
"""
    groups = ewkit.parse_sample_list(text)
    assert [g.heading for g in groups] == ["Vjets: Z+jets", "ttbar"]
    assert [d.campaign for d in groups[0].datasets] == ["mc23a", "mc23d"]
    assert ewkit.parse_sample_list("") == []


def test_sample_file_parts() -> None:
    assert ewkit.sample_file_parts("Run3_bkg_syst.txt") == ("Run3", "bkg_syst")
    assert ewkit.sample_file_parts("Run2_sig.txt") == ("Run2", "sig")
    assert ewkit.sample_file_parts("README.md") is None


def test_summarize_config() -> None:
    summary = ewkit.summarize_config(
        """Extend: '../CompressedSleptons/config.yaml'
CommonServices:
  runSystematics: False
Output:
  treeName: 'analysis'
AddConfigBlocks:
  - modulePath: 'TopCPToolkit.BoostedJetTaggersConfig'
    functionName: 'BoostedJetTaggersConfig'
"""
    )
    assert summary.extend == "../CompressedSleptons/config.yaml"
    assert summary.blocks == ["CommonServices", "Output", "AddConfigBlocks"]
    assert summary.output_tree == "analysis"
    assert ewkit.unavailable_modules(summary.added_block_modules) == ["TopCPToolkit.BoostedJetTaggersConfig"]
    assert ewkit.summarize_config("a: [").parse_error


def test_resolve_extend() -> None:
    assert (
        ewkit.resolve_extend("data/SleptonDijetFF/config.yaml", "../CompressedSleptons/config.yaml")
        == "data/CompressedSleptons/config.yaml"
    )
    assert ewkit.resolve_extend("data/X/config.yaml", "../../../etc/passwd") is None


def test_codeowners() -> None:
    rules = ewkit.parse_codeowners(
        """# Default Code Owner
* @bhodkins @miholzbo
/data/RadiativeDecays/ @bhodkins
/data/DiTaus/ @nifomin @chpotter
"""
    )
    assert ewkit.owners_for_path(rules, "data/DiTaus") == ["@nifomin", "@chpotter"]
    assert ewkit.owners_for_path(rules, "data/Other") == ["@bhodkins", "@miholzbo"]


def test_ref_slug() -> None:
    assert ewkit.ref_slug("master") == "master"
    assert ewkit.ref_slug("ditaus_r3_v02") == "ditaus-r3-v02"
    assert ewkit.ref_slug("v1.2") == "v1-2"
    assert ewkit.ref_slug("Feature/Foo_") == "feature-foo"

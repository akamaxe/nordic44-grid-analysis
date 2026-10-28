import pytest

from n44.io.psse_parser import UnsupportedRawError, parse_raw, read_raw, tokenize
from tests.conftest import FIXTURES


@pytest.fixture(scope="module")
def base_case():
    return read_raw(FIXTURES / "N44_BC.raw")


def test_tokenize_commas_quotes_and_comments():
    assert tokenize("  1, 'A/B, C' ,, 3.5 / comment, x") == ["1", "A/B, C", None, "3.5"]
    assert tokenize("1 'NAME X' 2.0") == ["1", "NAME X", "2.0"]
    assert tokenize("0 / END OF BUS DATA, BEGIN LOAD DATA") == ["0"]


def test_header(base_case):
    assert (base_case.rev, base_case.sbase, base_case.basfrq) == (33, 1000.0, 50.0)
    assert (base_case.xfrrat, base_case.nxfrat) == (0, 1)
    assert base_case.title[1].startswith("2015-08-12 NORDIC44_BZ")
    assert base_case.export_note.startswith("PSS(R)E-33.5")


def test_counts(base_case):
    sizes = {
        name: len(getattr(base_case, name))
        for name in (
            "bus",
            "load",
            "fixed_shunt",
            "generator",
            "branch",
            "transformer",
            "transformer3",
            "area",
            "zone",
            "owner",
            "switched_shunt",
        )
    }
    assert sizes == {
        "bus": 44,
        "load": 48,
        "fixed_shunt": 0,
        "generator": 80,
        "branch": 67,
        "transformer": 12,
        "transformer3": 0,
        "area": 10,
        "zone": 1,
        "owner": 1,
        "switched_shunt": 0,
    }
    assert base_case.bus.ide.value_counts().to_dict() == {1: 26, 2: 17, 3: 1}
    assert base_case.bus.baskv.value_counts().to_dict() == {420.0: 28, 300.0: 15, 135.0: 1}
    assert (base_case.load.scale == 0).sum() == 13


def test_totals(base_case):
    load, gen = base_case.load, base_case.generator
    assert load.pl.sum() == pytest.approx(38470.000, abs=1e-6)
    assert load.ql.sum() == pytest.approx(12644.474, abs=1e-6)
    assert gen.pg.sum() == pytest.approx(38490.002, abs=1e-6)
    assert gen.qg.sum() == pytest.approx(11205.523, abs=1e-6)
    assert base_case.area.pdes.sum() == pytest.approx(0.0, abs=1e-9)
    # Exchange loads of links inside the model come in pairs that cancel out
    pl = load.set_index(["i", "id"]).pl
    assert pl[(3020, "1")] + pl[(7000, "6")] == 0
    assert pl[(6701, "1")] + pl[(7100, "3")] == 0


def test_defaults_for_omitted_fields(base_case):
    gen, load = base_case.generator, base_case.load
    # Machine records stop after O1/F1: WMOD and WPF take their defaults
    assert (gen.wmod == 0).all() and (gen.wpf == 1.0).all()
    assert (gen.o2 == 0).all() and (gen.f2 == 1.0).all()
    assert load.dtypes["area"] == "int64"


def test_transformer_record(base_case):
    tr = base_case.transformer.set_index(["i", "j"]).loc[(5101, 5100)]
    assert (tr.cw, tr.cz, tr.cm, tr.nmetr, tr.cod1, tr.cont1) == (1, 1, 1, 2, 1, 5101)
    assert (tr.r1_2, tr.x1_2, tr.sbase1_2) == (0.0008, 0.0305, 1000.0)
    assert (tr.windv1, tr.windv2, tr.rata1) == (1.01275, 1.0, 2000.0)


def test_rejects_other_revisions():
    with pytest.raises(UnsupportedRawError, match="revision"):
        parse_raw("0, 100.0, 34, 0, 1, 50.0\ntitle\ntitle\n0 /\nQ\n")


def test_rejects_unsupported_section_with_data():
    text = (FIXTURES / "N44_BC.raw").read_text(encoding="latin-1")
    marker = "0 / END OF AREA DATA, BEGIN TWO-TERMINAL DC DATA\n"
    with pytest.raises(UnsupportedRawError, match="two_terminal_dc"):
        parse_raw(text.replace(marker, marker + "'DC1', 1, 0.0, 100.0, 500.0\n"))

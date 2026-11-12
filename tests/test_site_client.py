"""Offline tests for site_client: saved HTML fixtures + a fake requests layer (no network)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import site_client as sc  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
LOGIN, STORE, UTILITY, REJECTED = ((FIX / f"{n}.html").read_text(encoding="utf-8")
                                    for n in ("login", "store", "utility", "weight_rejected"))

EMPTY_TRUCK_CELL = "20:37</p></td><td style='width: 15%;'><p style='font-size: 0.8em;text-align: center;margin-top: 0;margin-bottom: 0;'></p>"
assert STORE.count(EMPTY_TRUCK_CELL) == 1
STORE_LOADED = STORE.replace(EMPTY_TRUCK_CELL, EMPTY_TRUCK_CELL[:-4] + "21:05</p>")  # carton 4 on the truck


class Resp:
    def __init__(self, text="", status_code=200):
        self.text, self.status_code = text, status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSite:
    """Stands in for requests: records every POSTed form and answers by NextStep."""

    def __init__(self, **pages):
        self.pages = {"Login": STORE, "PVListAll": STORE, "PVNuovoPacco": STORE, "PVUtility": UTILITY, **pages}
        self.posts, self.gets = [], []
        self.headers = {}

    def get(self, url, **kw):
        self.gets.append(url)
        return Resp(LOGIN)

    def post(self, url, data=None, **kw):
        self.posts.append(dict(data))
        return Resp(self.pages.get(data["NextStep"], "<html>ok</html>"))

    def steps(self):
        return [p["NextStep"] for p in self.posts]


@pytest.fixture
def site(monkeypatch):
    fake = FakeSite()
    monkeypatch.setattr(sc.requests, "Session", lambda: fake)
    monkeypatch.setattr(sc.requests, "get", fake.get)
    return fake


@pytest.fixture
def session(site):
    return sc.StoreSession("mi", "arese", "954", "secret")


# ---- login page ----


def test_list_stores(site):
    slug, stores = sc.list_stores("MI", " Arese ")
    assert slug == "arese"
    assert stores == [
        ("493", "CONAD/Viale dei Platani 6 1/Arese"),
        ("954", "EUROSPIN/Via Alfredo Papa 1 1/Arese"),
        ("1937", "LA GRANDE I/Via Giuseppe Eugenio Luraghi 11 1/Arese"),
    ]
    assert site.gets == [sc.city_url("mi", "arese")] and not site.posts
    for city in ("../../x", "a/b", "..", "x?y=1", "a%2Fb", ""):  # not one path segment: never requested
        with pytest.raises(sc.CityNotFound):
            sc.list_stores("mi", city)
    assert len(site.gets) == 1


def test_login_and_store_page(site, session):
    assert site.posts == [{"NrPV": "954", "CodiceAccesso": "secret", "NextStep": "Login"}]
    assert session.title() == "Arese/EUROSPIN/Via Alfredo Papa 1"
    cats = session.categories()
    assert len(cats) == 13 and cats[0] == ("18", "Tonno in scatola") and cats[-1] == ("32", "Varie")
    assert session.next_number() == "5" and session.kg_limits() == (1.0, 22.0)
    cartons = session.cartons()
    assert cartons == [
        sc.Carton("4", "12,00", "Tonno in scatola", "20:37", ""),
        sc.Carton("3", "12,00", "Legumi/verdure in scatola", "17:50", "17:52"),
        sc.Carton("2", "10,00", "Colazione e merenda", "17:49", "17:52"),
        sc.Carton("1", "12,00", "Tonno in scatola", "17:49", "17:52"),
    ]
    assert [c.loaded for c in cartons] == [False, True, True, True]
    session.page = '<input type="hidden" value="7" name="NumeroPrevisto"><input name=kili min=0.50 max=30.00>'
    assert session.next_number() == "7" and session.kg_limits() == (0.5, 30.0)  # any attribute order
    session.page = "<html><body>niente</body></html>"
    assert session.next_number() is None and session.kg_limits() == (sc.KG_MIN, sc.KG_MAX)
    assert not session.closed()
    # as served by the site for a store whose collection was declared finished: a banner, no new-carton form
    session.page = """<form action="/gnca/mi/rho/" method="post"><div><p style='background-color: silver;'>
				Hai dichiarato terminata la colletta.<br>Per poter inserire nuovi cartoni
		    <input type="submit" value=" aprila."><input type="hidden" value="PVTerminata" name="NextStep"></p></div></form>
		<table><tbody></tbody></table>"""
    assert session.closed() and session.categories() == [] and session.next_number() is None
    site.pages["Login"] = LOGIN  # bounced back to the login page
    with pytest.raises(sc.LoginFailed):
        sc.StoreSession("mi", "arese", "954", "wrong")


# ---- cartons ----


def test_add_carton(site, session):
    forecast = lambda n: STORE.replace('name="NumeroPrevisto" value="5"', f'name="NumeroPrevisto" value="{n}"')
    session.page = forecast(4)
    carton = session.add_carton("18", 12.0)
    assert site.posts[-1] == {"NumeroPrevisto": "4", "contenuto": "18", "kili": "12.00", "NextStep": "PVNuovoPacco"}
    assert carton == sc.Carton("4", "12,00", "Tonno in scatola", "20:37", "")
    session.page = forecast(3)
    assert session.add_carton("18", 12.0).number == "4"  # n. 3 went to another volunteer, ours became n. 4
    for cat, kg, number in (("18", 12.5, 4), ("19", 12.0, 4), ("18", 12.0, 5)):  # other weight, other content, missing
        session.page = forecast(number)
        with pytest.raises(sc.SiteError):
            session.add_carton(cat, kg)


def test_add_carton_weight_rejected_by_site(site, session):
    site.pages["PVNuovoPacco"] = REJECTED
    with pytest.raises(sc.WeightRejected) as e:
        session.add_carton("32", 1.5)
    assert (e.value.lo, e.value.hi) == (4.0, 14.0)
    assert site.steps().count("PVNuovoPacco") == 1 and not any("MediConfermati" in p for p in site.posts)
    session.page = STORE
    with pytest.raises(sc.SiteError):  # the fake answers the same page again, so the carton never shows up
        session.add_carton("32", 1.5, confirm=True)
    assert site.posts[-1] == {"NumeroPrevisto": "5", "contenuto": "32", "kili": "1.50", "NextStep": "PVNuovoPacco",
                              "MediConfermati": "MediConfermati"}  # like the site's "Confermare" button


def test_edit_carton(site, session):
    form = session.edit_form()
    assert site.posts[-1] == {"NextStep": "PVUtility"}
    assert form.cartons == [("1153", "4", "Tonno in scatola", "12.00")] and (form.kg_min, form.kg_max) == (1.0, 22.0)
    assert len(form.categories) == 13 and form.categories[7] == ("25", "Pasta")
    i = STORE.index("<tbody")  # the list after the change: carton 4 is now Pasta 10,50
    site.pages["PVListAll"] = STORE[:i] + STORE[i:].replace("12,00", "10,50", 1).replace("Tonno in scatola", "Pasta", 1)
    carton = session.edit_carton("1153", "25", 10.5)
    assert site.posts[-2] == {"NextStep": "PVModificaPacco", "NrPaccoModificato": "1153", "contenutoModificato": "25",
                              "kiliModificato": "10.50"}
    assert carton == sc.Carton("4", "10,50", "Pasta", "20:37", "")
    with pytest.raises(sc.SiteError):  # the list does not show this change
        session.edit_carton("1153", "26", 10.5)
    n = site.steps().count("PVModificaPacco")
    with pytest.raises(sc.SiteError):  # not an editable carton: nothing is sent
        session.edit_carton("999", "25", 10.5)
    assert site.steps().count("PVModificaPacco") == n
    site.pages["PVModificaPacco"] = REJECTED
    with pytest.raises(sc.WeightRejected):
        session.edit_carton("1153", "32", 1.5)
    site.pages["PVUtility"] = "<html>niente</html>"
    assert session.edit_form() is None


# ---- truck ----


def test_truck_form(site, session):
    form = session.truck_form()
    assert site.posts[-1] == {"NextStep": "PVUtility"}
    assert form == sc.TruckForm(pending=1, max_carton="4", count_min=1, count_max=1, first="4", last="4",
                                total_kg="12", trip="2", summary=[("1", "Tonno in scatola", "12,00")])
    site.pages["PVUtility"] = UTILITY.replace('value="1" name="OldNrCartoni"', 'value="0" name="OldNrCartoni"')
    assert session.truck_form() is None
    site.pages["PVUtility"] = (FIX / "utility_empty.html").read_text(encoding="utf-8")  # real page, all loaded
    assert session.truck_form() is None


def test_load_truck(site, session):
    form = session.truck_form()
    before = len(site.posts)
    with pytest.raises(ValueError):  # count out of range: nothing is sent
        session.load_truck(form, "AB123CD", 2)
    assert len(site.posts) == before
    with pytest.raises(sc.SiteError):  # refreshed list (STORE) still shows carton 4 unloaded
        session.load_truck(form, "AB123CD")
    site.pages["PVListAll"] = STORE_LOADED
    cartons = session.load_truck(form, "AB123CD")
    assert site.posts[-2] == {"NextStep": "PVNuovoCamion", "OldNrCartoni": "1", "MaxCartone": "4", "NrCartoni": "1",
                              "IdentificativoCamion": "2", "Veicolo": "AB123CD"}
    assert site.posts[-1] == {"NextStep": "PVListAll"}
    assert all(c.loaded for c in cartons)


def test_site_label_renamed_everywhere():
    assert sc._options(STORE, "contenuto")[2] == ("20", "Passata e sughi")
    assert sc._options(UTILITY, "contenutoModificato")[2] == ("20", "Passata e sughi")
    assert sc._text("<td>3) Conserve di  pomodoro e sughi 8.00 Kg</td>") == "3) Passata e sughi 8.00 Kg"

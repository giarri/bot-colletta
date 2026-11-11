"""Client for the Colletta Alimentare web app at https://www.itud.eu/gnca/<prov>/<city>/.

Plain PHP app: every action is a form-encoded POST to the same URL, selected by the
hidden field NextStep. Session is kept with the PHPSESSID cookie (requests.Session).
See README.md §2 for the reverse-engineered protocol.
"""

import html
import os
import re
from dataclasses import dataclass, field
from urllib.parse import quote

import requests

BASE = os.environ.get("SITE_BASE", "https://www.itud.eu/gnca").rstrip("/")
TIMEOUT = 20
UA = "bot-colletta/1.0"

KG_MIN, KG_MAX = 1.0, 22.0


class SiteError(Exception):
    pass


class CityNotFound(SiteError):
    pass


class LoginFailed(SiteError):
    """Wrong or expired access code."""


class WeightRejected(SiteError):
    """The site refused the carton: weight outside the limits it expects for that category."""

    def __init__(self, lo: float, hi: float):
        super().__init__(f"peso fuori dai limiti previsti ({lo:g}-{hi:g} kg)")
        self.lo, self.hi = lo, hi


@dataclass
class Carton:
    number: str
    kg: str
    content: str
    done: str
    truck: str

    @property
    def loaded(self) -> bool:
        """True when the carton has already been loaded on a truck (non-empty Camion cell)."""
        return bool(self.truck.strip())


@dataclass
class TruckForm:
    """The "carica camion" form (NextStep=PVNuovoCamion) of the Utility page."""

    pending: int  # OldNrCartoni: cartons not yet loaded
    max_carton: str  # MaxCartone: highest carton number
    count_min: int  # NrCartoni min
    count_max: int  # NrCartoni max
    first: str  # "Numerati da <first> a <last>"
    last: str
    total_kg: str  # as displayed by the site, e.g. "12"
    trip: str  # IdentificativoCamion value ("Nr Viaggio")
    summary: list[tuple[str, str, str]] = field(default_factory=list)  # (nr, prodotto, kg)


@dataclass
class EditForm:
    """The "Modifica cartone" form (NextStep=PVModificaPacco) of the Utility page."""

    cartons: list[tuple[str, str, str, str]]  # (site id, carton number, content, kg) of the cartons not yet loaded
    categories: list[tuple[str, str]]  # (id, label) accepted as new content
    kg_min: float  # limits of the new-weight input
    kg_max: float


# the site's "weight outside the limits expected for this content" answer
LIMITS_RE = re.compile(r"(?is)limiti\s+previsti.{0,40}?maggiore\s+di\s*([\d.,]+)\s*e\s+minore\s+di\s*([\d.,]+)")


# Category labels we show under a different name than the site's. Applied to every text read from the site
# (select options, carton rows, truck summary), so the labels still compare equal to each other.
RENAMES = [(re.compile(r"(?i)conserve di pomodoro e sughi"), "Passata e sughi")]


def _text(fragment: str) -> str:
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"(?s)<[^>]+>", " ", fragment))).strip()
    for pattern, name in RENAMES:
        text = pattern.sub(name, text)
    return text


def _options(page: str, select_name: str) -> list[tuple[str, str]]:
    m = re.search(rf'(?is)<select[^>]*name="{select_name}"[^>]*>(.*?)</select>', page)
    if not m:
        return []
    return [(v, _text(lbl)) for v, lbl in re.findall(r'(?is)<option[^>]*value="(\d+)"[^>]*>(.*?)</option>', m.group(1))]


def _inputs(fragment: str) -> dict[str, dict[str, str]]:
    """{name: {attr: value}} for every named <input> (any attribute order, quoted or unquoted values)."""
    out = {}
    for tag in re.findall(r"(?is)<input\b([^>]*)>", fragment):
        attrs = {
            k.lower(): html.unescape(dq or sq or bare)
            for k, dq, sq, bare in re.findall(r"""(?s)([\w:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?""", tag)
        }
        if attrs.get("name"):
            out.setdefault(attrs["name"], attrs)
    return out


def _form(page: str, next_step: str) -> str | None:
    """Inner HTML of the <form> whose NextStep input has the given value."""
    for body in re.findall(r"(?is)<form\b[^>]*>(.*?)</form>", page):
        if _inputs(body).get("NextStep", {}).get("value") == next_step:
            return body
    return None


def _num(value: str | None, default, cast=int):
    try:
        return cast(value.strip().replace(",", "."))
    except (AttributeError, ValueError):
        return default


def city_url(prov: str, city_slug: str) -> str:
    return f"{BASE}/{quote(prov, safe='')}/{quote(city_slug, safe='')}/"


def _slug_candidates(city: str) -> list[str]:
    c = re.sub(r"\s+", " ", city.strip().lower())
    out = [c]
    if " " in c:
        out += [c.replace(" ", ""), c.replace(" ", "-"), c.replace(" ", "_")]
    return list(dict.fromkeys(out))


def list_stores(prov: str, city: str) -> tuple[str, list[tuple[str, str]]]:
    """Return (city_slug, [(pv_id, label), ...]) or raise CityNotFound."""
    prov = prov.strip().lower()
    # one path segment each, so that the bot can only ever talk to .../gnca/<prov>/<city>/
    if not re.fullmatch(r"[a-z]{2}", prov) or not re.fullmatch(r"[\w' .-]+", city.strip()) or not city.strip(". "):
        raise CityNotFound(f"{prov}/{city}")
    for slug in _slug_candidates(city):
        r = requests.get(city_url(prov, slug), timeout=TIMEOUT, headers={"User-Agent": UA})
        if r.status_code == 404:
            continue
        r.raise_for_status()
        stores = _options(r.text, "NrPV")
        if stores:
            return slug, stores
    raise CityNotFound(f"{prov}/{city}")


class StoreSession:
    """A logged-in session for one store (PV)."""

    def __init__(self, prov: str, city_slug: str, pv_id: str, code: str):
        self.url = city_url(prov, city_slug)
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.page = ""
        self._login(pv_id, code)

    def _post(self, **data) -> str:
        r = self.s.post(self.url, data=data, timeout=TIMEOUT)
        r.raise_for_status()
        if 'name="CodiceAccesso"' in r.text:  # bounced back to the login page
            raise LoginFailed()
        self.page = r.text
        return r.text

    def _login(self, pv_id: str, code: str) -> None:
        self.s.get(self.url, timeout=TIMEOUT).raise_for_status()  # obtain PHPSESSID
        self._post(NrPV=pv_id, CodiceAccesso=code, NextStep="Login")

    # ---- parsing helpers on the current page ----

    def title(self) -> str:
        m = re.search(r"(?is)<title>(.*?)</title>", self.page)
        return _text(m.group(1)) if m else ""

    def categories(self) -> list[tuple[str, str]]:
        return _options(self.page, "contenuto")

    def next_number(self) -> str | None:
        value = _inputs(self.page).get("NumeroPrevisto", {}).get("value", "").strip()
        return value if value.isdigit() else None

    def kg_limits(self) -> tuple[float, float]:
        """(min, max) of the `kili` input on the current page; falls back to (KG_MIN, KG_MAX)."""
        a = _inputs(self.page).get("kili", {})
        return _num(a.get("min"), KG_MIN, float), _num(a.get("max"), KG_MAX, float)

    def cartons(self) -> list[Carton]:
        m = re.search(r"(?is)<tbody>(.*?)</tbody>", self.page)
        if not m:
            return []
        out = []
        for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", m.group(1)):
            cells = [_text(c) for c in re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)]
            if len(cells) >= 5:
                out.append(Carton(*cells[:5]))
        return out

    # ---- actions ----

    def refresh(self) -> list[Carton]:
        self._post(NextStep="PVListAll")
        return self.cartons()

    def add_carton(self, category_id: str, kg: float, confirm: bool = False) -> Carton:
        """Register a new carton. Returns the carton row as shown by the site.

        The site checks per-category weight limits server-side: out of range, it saves nothing and answers
        with a "confirm anyway?" form (hidden MediConfermati). Then: confirm=False raises WeightRejected,
        confirm=True re-submits that form like the site's "Confermare" button.

        The number is only the site's forecast: other volunteers register cartons on the same store, so the new
        carton is recognised by content and weight too, at that number or a later one.
        """
        number = self.next_number()
        if not number:
            raise SiteError("NumeroPrevisto non trovato nella pagina")
        label = dict(self.categories()).get(category_id)
        data = dict(NumeroPrevisto=number, contenuto=category_id, kili=f"{kg:.2f}", NextStep="PVNuovoPacco")
        self._post(**data)
        m = LIMITS_RE.search(self.page)
        if m and "MediConfermati" in self.page:
            if not confirm:
                raise WeightRejected(_num(m.group(1), KG_MIN, float), _num(m.group(2), KG_MAX, float))
            self._post(**data, MediConfermati="MediConfermati")
        mine = [c for c in self.cartons()
                if c.number.isdigit() and int(c.number) >= int(number) and abs(_num(c.kg, -1.0, float) - kg) < 0.005
                and (label is None or c.content == label)]
        if mine:
            return min(mine, key=lambda c: int(c.number))
        raise SiteError("Il cartone non risulta nella lista dopo l'invio: " + _text(self.page)[:300])

    def edit_form(self) -> EditForm | None:
        """POST NextStep=PVUtility (read-only) and parse the edit form. None when no carton can be edited."""
        form = _form(self._post(NextStep="PVUtility"), "PVModificaPacco")
        if form is None:
            return None
        cartons = []
        for cid, label in _options(form, "NrPaccoModificato"):  # "4) Tonno in scatola 12.00 Kg"
            m = re.fullmatch(r"(?i)(\d+)\)\s*(.*?)\s+([\d.,]+)\s*kg\.?", label)
            if m:
                cartons.append((cid, m.group(1), m.group(2), m.group(3)))
        if not cartons:
            return None
        kg = _inputs(form).get("kiliModificato", {})
        return EditForm(cartons, _options(form, "contenutoModificato"),
                        _num(kg.get("min"), KG_MIN, float), _num(kg.get("max"), KG_MAX, float))

    def edit_carton(self, carton_id: str, category_id: str, kg: float) -> Carton:
        """Change content and weight of a carton not yet loaded. Returns the carton row as shown by the site.

        The PVModificaPacco response format is unknown: the change is verified on the refreshed carton list.
        """
        form = self.edit_form()
        entry = next((c for c in (form.cartons if form else []) if c[0] == carton_id), None)
        if entry is None:
            raise SiteError("il cartone non è più modificabile (forse è già sul camion)")
        label = dict(form.categories).get(category_id)
        if label is None:
            raise SiteError("contenuto non previsto dal sito")
        self._post(NextStep="PVModificaPacco", NrPaccoModificato=carton_id, contenutoModificato=category_id, kiliModificato=f"{kg:.2f}")
        m = LIMITS_RE.search(self.page)
        if m:
            raise WeightRejected(_num(m.group(1), KG_MIN, float), _num(m.group(2), KG_MAX, float))
        for c in self.refresh():
            if c.number == entry[1] and c.content == label and abs(_num(c.kg, -1.0, float) - kg) < 0.005:
                return c
        raise SiteError("La modifica non risulta nella lista dopo l'invio: " + _text(self.page)[:300])

    def truck_form(self) -> TruckForm | None:
        """POST NextStep=PVUtility (read-only) and parse the truck form. None when there is nothing to load."""
        form = _form(self._post(NextStep="PVUtility"), "PVNuovoCamion")
        if form is None:
            return None
        inp = _inputs(form)

        def val(name: str) -> str:
            return inp.get(name, {}).get("value", "").strip()

        pending = _num(val("OldNrCartoni"), 0)
        nr = inp.get("NrCartoni", {})
        count_max = _num(nr.get("max"), pending)
        if pending < 1 or count_max < 1:
            return None
        rng = re.search(r"(?is)Numerati\s+da\s*<b>(.*?)</b>\s*a\s*<b>(.*?)</b>", form)
        tot = re.search(r"(?is)totale\s+di\s*<b>(.*?)</b>", form)
        summary = []
        for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", form[form.lower().rfind("<table"):]):
            cells = [_text(c) for c in re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)]
            if len(cells) == 3 and cells[0].isdigit():  # skips the Nr/Prodotto/Kg. header
                summary.append((cells[0], cells[1], cells[2]))
        return TruckForm(
            pending=pending,
            max_carton=val("MaxCartone"),
            count_min=_num(nr.get("min"), 1),
            count_max=count_max,
            first=_text(rng.group(1)) if rng else "",
            last=_text(rng.group(2)) if rng else "",
            total_kg=_text(tot.group(1)) if tot else "",
            trip=val("IdentificativoCamion"),
            summary=summary,
        )

    def load_truck(self, form: TruckForm, plate: str, count: int | None = None) -> list[Carton]:
        """Mark `count` cartons (default: all pending) as loaded on the truck. Returns the refreshed list."""
        plate = plate.strip()
        count = form.count_max if count is None else count
        if not form.count_min <= count <= form.count_max:
            raise ValueError("count out of range")
        answer = self._post(
            NextStep="PVNuovoCamion",
            OldNrCartoni=str(form.pending),
            MaxCartone=form.max_carton,
            NrCartoni=str(count),
            IdentificativoCamion=form.trip,
            Veicolo=plate,
        )
        # The PVNuovoCamion response format is unknown: verify on the refreshed carton list instead.
        cartons = self.refresh()
        if sum(not c.loaded for c in cartons) > form.pending - count:
            raise SiteError("I cartoni non risultano caricati sul camion dopo l'invio: " + _text(answer)[:300])
        return cartons

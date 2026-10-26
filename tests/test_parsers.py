"""Testy na syntetycznych danych odwzorowujących strukturę OtoMoto/OLX z 2026-10-24."""
import json

from hunter.sources import olx, otomoto


def _page(page_props: dict) -> str:
    nd = {"props": {"pageProps": page_props}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></html>'


NODE = {
    "id": "6151218792", "title": "Kia Stinger", "createdAt": "2026-10-20T08:39:50Z",
    "shortDescription": "Lekko uszkodzony", "url": "https://www.otomoto.pl/osobowe/oferta/kia-stinger-ID6Hx.html",
    "location": {"city": {"name": "Wrocław"}, "region": {"name": "Dolnośląskie"}},
    "thumbnail": {"x1": "https://ireland.apollo.olxcdn.com/v1/files/abc/image;s=320x240"},
    "price": {"amount": {"units": 52000, "value": "52000", "currencyCode": "PLN"}},
    "parameters": [
        {"key": "make", "value": "kia"}, {"key": "model", "value": "stinger"},
        {"key": "fuel_type", "value": "diesel"}, {"key": "gearbox", "value": "automatic"},
        {"key": "mileage", "value": "123456"}, {"key": "engine_capacity", "value": "2199"},
        {"key": "engine_power", "value": "200"}, {"key": "year", "value": "2018"},
    ],
    "seller": {"__typename": "PrivateSeller"},
    "priceEvaluation": {"indicator": "NONE"}, "cepikVerified": False,
}


def test_otomoto_search():
    data = {"advertSearch": {"totalCount": 70, "pageInfo": {"pageSize": 32, "currentOffset": 0},
                             "edges": [{"node": NODE}]}}
    html = _page({"urqlState": {"123": {"data": json.dumps(data)}, "456": {"data": "{}"}}})
    total, size, items = otomoto.parse_search(html)
    assert (total, size, len(items)) == (70, 32, 1)
    it = items[0]
    assert it["make"] == "kia" and it["model"] == "stinger"
    assert it["power_hp"] == 200 and it["mileage_km"] == 123456 and it["year"] == 2018
    assert it["price"] == 52000 and it["currency"] == "PLN"
    assert it["seller_type"] == "private" and it["city"] == "Wrocław"


def test_otomoto_search_params():
    p = otomoto.search_params({"filter_enum_damaged": "1", "filter_float_price:to": 50000, "x": None}, page=3)
    assert p == {"search[filter_enum_damaged]": "1", "search[filter_float_price:to]": "50000", "page": "3"}


def test_otomoto_detail():
    pd = {k: {"label": k, "values": [{"value": v, "label": v}]} for k, v in {
        "body_type": "combi", "vin": "KNAE251LDJ6012345", "damaged": "1", "is_imported_car": "0",
        "has_registration": "1", "rhd": "0"}.items()}
    advert = {"parametersDict": pd, "status": "ACTIVE",
              "images": {"photos": [{"id": "1", "url": "https://ireland.apollo.olxcdn.com/v1/files/x/image"}]},
              "description": "<p>Uszkodzony <b>przód</b></p><p>Poduszki całe</p>", "equipment": []}
    d = otomoto.parse_detail(_page({"advert": advert}))
    assert d["body_type"] == "combi" and d["vin"] == "KNAE251LDJ6012345"
    assert d["damaged"] is True and d["imported"] is False and d["rhd"] is False
    assert d["photos"][0].endswith("/image;s=1280x960")
    assert "Uszkodzony przód" in d["description"] and "<" not in d["description"]


OLX_OFFER = {
    "id": 999, "url": "https://www.olx.pl/d/oferta/x-CID5-ID1.html", "title": "Kia Stinger GT",
    "created_time": "2026-10-24T10:00:00+02:00", "description": "Auto po szkodzie, USA, Copart",
    "business": False, "category": {"id": 192, "type": "automotive"},
    "location": {"city": {"name": "Lubin"}, "region": {"name": "Dolnośląskie"}},
    "photos": [{"id": 1, "link": "https://ireland.apollo.olxcdn.com:443/v1/files/a-PL/image;s={width}x{height}"}],
    "params": [
        {"key": "price", "value": {"value": 39900, "currency": "PLN"}},
        {"key": "model", "value": {"key": "stinger", "label": "Stinger"}},
        {"key": "year", "value": {"key": "2019", "label": "2019"}},
        {"key": "petrol", "value": {"key": "petrol", "label": "Benzyna"}},
        {"key": "car_body", "value": {"key": "sedan", "label": "Sedan"}},
        {"key": "enginesize", "value": {"key": "3342", "label": "3342 cm³"}},
        {"key": "condition", "value": {"key": "damaged", "label": "Uszkodzony"}},
        {"key": "transmission", "value": {"key": "automatic", "label": "Automatyczna"}},
        {"key": "enginepower", "value": {"key": "366", "label": "366 KM"}},
        {"key": "milage", "value": {"key": "80000", "label": "80 000 km"}},
        {"key": "drive", "value": {"key": "all-wheel-auto", "label": "4x4"}},
        {"key": "vin", "value": {"key": "KNAE55LC1K6012345", "label": "KNAE55LC1K6012345"}},
    ],
}


def test_olx_normalize():
    it = olx.normalize(OLX_OFFER, {192: "kia"})
    assert it["make"] == "kia" and it["model"] == "stinger" and it["power_hp"] == 366
    assert it["price"] == 39900 and it["mileage_km"] == 80000 and it["damaged"] is True
    assert it["photos"][0].endswith(";s=1280x960") and it["vin"] == "KNAE55LC1K6012345"
    assert it["body_type"] == "sedan" and it["gearbox"] == "automatic"


def test_olx_duplicate_detection():
    assert olx.is_otomoto_duplicate({"partner": {"code": "otomoto_pl_form"}})
    assert olx.is_otomoto_duplicate({"external_url": "https://otomoto.pl/x"})
    assert not olx.is_otomoto_duplicate({"partner": None})


class FakeHttp:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, params=None, **kw):
        page = int((params or {}).get("page", 1))
        self.calls.append(page)
        return self.pages.get(page)


def _search_page(ids, total):
    edges = [{"node": dict(NODE, id=str(i))} for i in ids]
    data = {"advertSearch": {"totalCount": total, "pageInfo": {"pageSize": 2}, "edges": edges}}
    return _page({"urqlState": {"k": {"data": json.dumps(data)}}})


def test_otomoto_pagination_and_incremental_stop():
    pages = {1: _search_page([1, 2], 6), 2: _search_page([3, 4], 6), 3: _search_page([5, 6], 6)}
    http = FakeHttp(pages)
    assert [i["source_id"] for i in otomoto.search(http, {}, 10)] == ["1", "2", "3", "4", "5", "6"]
    assert http.calls == [1, 2, 3]
    known = {"1", "2", "3", "4", "5", "6"}
    http2 = FakeHttp(pages)
    stop = lambda items: all(i["source_id"] in known for i in items)  # noqa: E731
    list(otomoto.search(http2, {}, 10, stop_when_known=stop))
    assert http2.calls == [1]


def test_otomoto_cepik_parsed():
    pd = {k: {"label": k, "values": [{"value": v, "label": v}]} for k, v in {"damaged": "1"}.items()}
    cepik = {"verifiedCar": True, "numberOfOwners": {"value": "2"}, "importedCar": {"value": True},
             "insuranceValidity": {"value": "13.08.2027"}, "inspectionValidity": {"value": "26.03.2027"},
             "timeline": [
                 {"type": "periodic_technical_inspection", "date": "26.03.2026", "details": {"result": "positive"}},
                 {"type": "periodic_technical_inspection", "date": "20.03.2026", "details": {"result": "negative"}},
                 {"type": "first_registration_in_poland", "date": "05.04.2019", "details": {}},
                 {"type": "first_registration_abroad", "date": "01.02.2017", "details": {}}]}
    html = _page({"advert": {"parametersDict": pd, "images": {"photos": []}, "description": "", "equipment": []},
                  "cepikWidget": {"status": 0, "props": {"advert": {"cepik": cepik}}}})
    c = otomoto.parse_detail(html)["cepik"]
    assert c["owners"] == 2 and c["inspections"] == 2 and c["failed_inspections"] == 1
    assert c["first_registration_abroad"] == "01.02.2017" and c["insurance_valid_until"] == "13.08.2027"
    none = _page({"advert": {"parametersDict": pd, "images": {"photos": []}, "description": "", "equipment": []},
                  "cepikWidget": {"status": 1}})
    assert otomoto.parse_detail(none)["cepik"] is None

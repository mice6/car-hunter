# car-hunter

🇬🇧 [English README](README.md)

Monitor uszkodzonych aut z OtoMoto i OLX: zbiera ogłoszenia, wycenia zdrowy odpowiednik na podstawie
rynku, szacuje naprawę i ryzyko, a najlepsze okazje wysyła na Telegram.

Domyślne kryteria (`config.example.yaml`): automat, min. 170 KM, budżet łącznie 55 000 zł,
premia za SUV/kombi i diesla.

## Jak to działa

```
candidates  →  enrich  →  market  →  evaluate  →  alert
 uszkodzone     VIN,       zdrowe auta   wycena +      Telegram
 (OtoMoto+OLX)  zdjęcia,   tych modeli   naprawa +
                opis       (do wyceny)   ryzyko
```

- **OtoMoto**: dane z osadzonego stanu Next.js (`__NEXT_DATA__`), lista + strona ogłoszenia.
- **OLX**: publiczne JSON API `/api/v1/offers/`; duplikaty z OtoMoto są pomijane.
- **Wycena**: regresja log(ceny) po roczniku, przebiegu i mocy na zdrowych ogłoszeniach tego samego
  modelu (≥15 porównań), inaczej ważona mediana najbliższych. Ceny ofertowe × 0.93.
- **Naprawa (na razie)**: heurystyka ze słów kluczowych (lekka 6% / średnia 15% / przód 20% / ciężka 35%
  wartości). To jest najsłabsze ogniwo i zostanie zastąpione modelem vision (krok 3).
- **Ryzyko**: import z USA, zalanie, ciężka szkoda, brak rejestracji, mało zdjęć.
- **Score** = oszczędność × (1 − ryzyko) + premie za preferencje.

## Uruchomienie (Docker LXC na Proxmoxie)

```bash
git clone <repo> car-hunter && cd car-hunter
cp config.example.yaml config.yaml
cp .env.example .env         # hasło do bazy + token bota Telegram i chat_id
docker compose up -d --build
docker compose logs -f hunter
```

Pierwsze uruchomienie: pełny przebieg kandydatów, pobranie szczegółów i rynku dla wszystkich modeli
potrwa kilka godzin (celowo powolne zapytania). Kolejne cykle co 30 min są przyrostowe.

Ręcznie:

```bash
docker compose run --rm hunter candidates --full
docker compose run --rm hunter enrich --limit 300
docker compose run --rm hunter market --max-models 50
docker compose run --rm hunter evaluate
docker compose run --rm hunter top -n 20
```

Baza jest wystawiona na `127.0.0.1:5433` (DBeaver, Metabase, n8n).

## Ważne

- Scrapuj z domowego IP, powoli (domyślnie 2.5–6 s między zapytaniami). Regulaminy portali
  zabraniają masowego pobierania danych; to narzędzie do użytku własnego.
- Struktura OtoMoto/OLX zweryfikowana 2026-10-24. Gdy portal ją zmieni, padną parsery
  (`hunter/sources/*`) i testy w `tests/` pokażą gdzie.
- Wynik to filtr okazji, nie decyzja. Każde auto z alertu i tak trzeba sprawdzić: VIN w historii
  pojazdu / aukcjach USA, oględziny, pomiar lakieru, diagnostyka.

## Testy

```bash
pip install -r requirements.txt pytest
pytest -q                                            # parsery i scoring
HUNTER_TEST_DB=postgresql://... pytest -q            # + warstwa bazy (czyści schemat!)
```

## Roadmapa

1. ✅ Scraper OtoMoto + OLX, baza z historią cen, wycena z porównań, scoring, alerty Telegram.
2. Globalny model wyceny (CatBoost) na całym rynku zdrowych aut z wersją, wyposażeniem i regionem;
   pozwoli wyceniać rzadkie wersje i wykrywać zaniżone ceny także wśród aut nieuszkodzonych.
3. Analiza zdjęć na **RunPod Serverless**: open-source'owy model vision (Qwen-VL) przegląda wszystkie
   zdjęcia kandydatów i zwraca JSON (strefy, elementy do wymiany, poduszki, konstrukcja, zalanie,
   czy jeździ). Wynik trafia do `listings.vision` i automatycznie zastępuje heurystykę.
   Na najlepsze ~5% dodatkowo głęboka analiza przez Claude + lista pytań do sprzedawcy.
4. Panel z rankingiem i historią cen (Symfony albo prosty dashboard).

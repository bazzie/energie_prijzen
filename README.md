# Energie Prijzen (dynamisch Vattenfall-contract)

[![Tests](https://github.com/bazzie/energie_prijzen/actions/workflows/tests.yml/badge.svg)](https://github.com/bazzie/energie_prijzen/actions/workflows/tests.yml)

Home Assistant custom integration die de dynamische stroom- en gasprijzen ophaalt
en omrekent naar de **all-in prijs** van een dynamisch energiecontract van
Vattenfall in Nederland.

Vattenfall heeft geen publieke API voor dynamische tarieven. De all-in prijs wordt
daarom berekend uit de day-ahead marktprijs (via de publieke
[EnergyZero](https://www.energyzero.nl)-API) plus jouw contractgegevens:

```
all-in prijs = (marktprijs + opslag + energiebelasting) × (1 + btw)
```

- **Stroom:** prijs per kwartier (EUR/kWh).
- **Gas:** prijs per gasdag van 06:00 tot 06:00 (EUR/m³).

Er staan geen belastingtarieven in de code. De energiebelasting kan automatisch uit
de prijsbron komen of je vult hem zelf in. De opslag vul je altijd zelf in.

## Installatie

### Via HACS (aanbevolen)

1. Open HACS → menu (⋮) → **Aangepaste repositories**.
2. Voeg de URL van deze repository toe met type **Integratie**.
3. Zoek **Energie Prijzen**, installeer en herstart Home Assistant.

### Handmatig

1. Kopieer de map `custom_components/energie_prijzen` naar
   `<config>/custom_components/energie_prijzen`.
2. Herstart Home Assistant.

Je hebt Home Assistant 2026.11 of nieuwer nodig.

## Configuratie

Ga naar **Instellingen → Apparaten & diensten → Integratie toevoegen** en kies
**Energie Prijzen**. Vul in (alle bedragen **exclusief btw**):

| Veld | Eenheid | Toelichting |
|------|---------|-------------|
| Opslag stroom | EUR/kWh | Inkoopvergoeding/opslag van Vattenfall per kWh |
| Opslag gas | EUR/m³ | Opslag van Vattenfall per m³ |
| Energiebelasting automatisch bepalen | aan/uit | Standaard aan, zie hieronder |
| Energiebelasting stroom | EUR/kWh | Verplicht als automatisch uit staat, anders optionele terugval |
| Energiebelasting gas | EUR/m³ | Verplicht als automatisch uit staat, anders optionele terugval |
| Btw | % | Standaard 21 |

### Automatische energiebelasting

EnergyZero levert per interval een kale marktprijs en een all-in prijs. Het verschil
daartussen (exclusief btw) is de energiebelasting:

| Periode | Stroom (EUR/kWh) | Gas (EUR/m³) |
|---------|------------------|--------------|
| 2025 | 0,10154 | 0,57816 |
| 2026 | 0,09161 | 0,60066 |

Met de automatische optie gebruikt de integratie deze waarde. Na een tariefwijziging,
zoals op 1 januari, verandert de waarde dan vanzelf mee.

Vul je ook handmatige waarden in? Dan dienen die als terugval voor intervallen
waarvoor de bron geen energiebelasting levert. Is er voor een energiesoort helemaal
geen energiebelasting bekend, dan wordt dat als fout behandeld en blijven de laatst
bekende prijzen in gebruik.

In de attributen en in diagnostics zie je of de waarde automatisch of handmatig is
bepaald (`energy_tax_source`).

De vermindering energiebelasting (een vast bedrag per jaar) en de lagere tarieven
boven 10.000 kWh of 1.000 m³ zitten niet in de prijs per kWh.

De actuele bedragen vind je in je contract of tariefoverzicht van Vattenfall, en
voor de energiebelasting op de website van de Belastingdienst. Let op: tarieven
worden vaak *inclusief* btw gepubliceerd. Deel die dan door 1,21.

Bij de installatie wordt gecontroleerd of de prijsbron bereikbaar is.

**Later aanpassen:** ga naar **Instellingen → Apparaten & diensten → Energie
Prijzen → Configureren**. De integratie herlaadt dan automatisch.

## Sensoren

Je krijgt twee apparaten met deze sensoren.

**Dynamische stroom**

| Sensor | Beschrijving |
|--------|--------------|
| Huidige prijs | All-in prijs van het huidige kwartier (EUR/kWh, `state_class: measurement`) |
| Volgende prijs | All-in prijs van het volgende kwartier |
| Gemiddelde / Laagste / Hoogste prijs vandaag | Statistiek over vandaag (all-in) |
| Goedkoopste / Duurste moment vandaag | Starttijd van het goedkoopste/duurste kwartier (timestamp) |
| Marktprijs incl. btw | Kale marktprijs × (1 + btw), zonder opslag en energiebelasting (ter controle) |
| Energiebelasting incl. btw | Energiebelasting van het huidige kwartier × (1 + btw) |

**Dynamisch gas**

| Sensor | Beschrijving |
|--------|--------------|
| Huidige gasprijs | All-in prijs van de huidige gasdag (EUR/m³) |
| Volgende gasprijs | All-in prijs van de volgende gasdag (zodra bekend) |
| Marktprijs gas incl. btw | Kale gasprijs × (1 + btw) |
| Energiebelasting gas incl. btw | Energiebelasting per m³ × (1 + btw) |

De energiebelasting-sensoren hebben de attributen `price_excl_vat` en
`energy_tax_source` (`automatic` of `manual`).

De sensoren wisselen elk kwartier van waarde zonder extra API-calls. Lukt het
ophalen tijdelijk niet, dan blijven de sensoren werken met de laatst opgehaalde
prijzen, zolang die het huidige moment nog dekken.

### Attributen voor grafieken en automations

De sensoren **Huidige prijs** en **Huidige gasprijs** hebben deze attributen.

De prijsopbouw van het huidige interval (alle bedragen in EUR per eenheid):

| Attribuut | Betekenis |
|-----------|-----------|
| `market_price` | Marktprijs, excl. btw |
| `surcharge` | Opslag, excl. btw |
| `energy_tax` | Energiebelasting, excl. btw |
| `energy_tax_source` | `automatic` of `manual` |
| `price_excl_vat` | Som van de drie bedragen hierboven |
| `vat` | Btw-bedrag |

De status van de sensor is `price_excl_vat + vat`.

De prijslijst:

- `prices`: een lijst met `{start, end, price}` (ISO 8601, lokale tijd, all-in
  prijs) voor vandaag en, zodra beschikbaar, morgen;
- `tomorrow_available`: `true` als de prijzen van morgen compleet zijn.

Het attribuut `prices` wordt niet in de database (recorder) opgeslagen.

Voorbeeld voor [ApexCharts card](https://github.com/RomRider/apexcharts-card):

```yaml
type: custom:apexcharts-card
graph_span: 2d
span:
  start: day
now:
  show: true
series:
  - entity: sensor.dynamische_stroom_huidige_prijs
    type: column
    data_generator: |
      return entity.attributes.prices.map(p => [new Date(p.start).getTime(), p.price]);
```

Voorbeeld van een template om de goedkoopste 4 kwartieren van vandaag en morgen te
vinden:

```jinja
{{ state_attr('sensor.dynamische_stroom_huidige_prijs', 'prices')
   | sort(attribute='price') | map(attribute='start') | list | first(4) }}
```

Het entity-ID hangt af van de taal van je installatie. Controleer het onder
**Instellingen → Entiteiten**.

### Energy dashboard

Ga naar **Instellingen → Dashboards → Energie → Elektriciteitsnet → Netverbruik**.
Kies daar **Gebruik een entiteit met de huidige prijs** en selecteer
*Dynamische stroom Huidige prijs*. Voor gas kies je *Dynamisch gas Huidige
gasprijs*.

## Wanneer worden prijzen opgehaald?

- De prijzen van morgen verschijnen meestal tussen 14:00 en 15:00. Vanaf 14:00
  (plus een willekeurige vertraging van 0 tot 5 minuten) probeert de integratie ze
  op te halen.
- Zijn ze er nog niet, of gaat er iets mis? Dan probeert de integratie het opnieuw
  na 5, 10, 20 en 30 minuten, en daarna elk uur.
- Als alles bekend is, haalt de integratie maximaal elke 12 uur nieuwe data op. Dat
  gebeurt ook zodra de bekende gasprijs afloopt (om 06:00).
- Alle daglogica gebruikt de tijdzone Europe/Amsterdam. Dagen van 23 en 25 uur
  (zomer- en wintertijd) hebben 92 en 100 kwartieren.

## Diagnostics

Via **Apparaten & diensten → Energie Prijzen → ⋮ → Diagnostische gegevens
downloaden** krijg je een overzicht met:

- de instellingen;
- het aantal intervallen en de huidige prijsopbouw;
- het laatste en het volgende ophaalmoment;
- eventuele fouten.

## Andere prijsbron

De databron zit achter de abstracte klasse `PriceSource` in `api.py`. Wil je een
andere bron gebruiken, zoals ENTSO-E? Implementeer dan `async_get_prices()`, die
marktprijzen exclusief btw teruggeeft als `MarketPrice(start, end, price)`. Laat
daarna `create_price_source()` die bron teruggeven. De rest van de integratie
hoeft niet te veranderen.

Kent de bron de energiebelasting? Zet dan `provides_energy_tax = True` en vul
`MarketPrice.energy_tax` in. ENTSO-E kent geen energiebelasting; daar vul je die
handmatig in.

## Ontwikkeling

```bash
python3.14 -m venv .venv && source .venv/bin/activate
pip install -r requirements_test.txt
pytest
ruff format . && ruff check .
```

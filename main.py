from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from datetime import date, datetime
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


CNB_DAILY_RATES_URL = (
    "https://www.cnb.cz/en/financial-markets/foreign-exchange-market/"
    "central-bank-exchange-rate-fixing/central-bank-exchange-rate-fixing/daily.txt"
)
DEFAULT_DPH_RATE = Decimal("12")
DB_PATH = Path(__file__).with_name("bookings.db")
DEFAULT_CARD_FEES = {
    "TEST": Decimal("1.50"),
    "mastercard": Decimal("1.50"),
    "cash": Decimal("0"),
}

BOOKING_COLUMNS = (
    "id",
    "booking_date",
    "guest_name",
    "property_name",
    "nights",
    "guests",
    "price_mode",
    "price_per_night",
    "currency",
    "gross_foreign",
    "czk_rate",
    "gross_czk",
    "dph_rate",
    "dph_amount",
    "revenue_without_dph",
    "card_type",
    "card_fee_rate",
    "card_fee",
    "variable_costs",
    "net_profit",
    "rates_date",
    "created_at",
)


@dataclass(frozen=True)
class CurrencyRate:
    code: str
    amount: Decimal
    rate_czk: Decimal
    name: str

    @property
    def czk_per_unit(self) -> Decimal:
        return self.rate_czk / self.amount


def money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def percent(value: Decimal, rate: Decimal) -> Decimal:
    return value * rate / Decimal("100")


def today_iso() -> str:
    return date.today().isoformat()


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                booking_date TEXT NOT NULL,
                guest_name TEXT NOT NULL,
                property_name TEXT NOT NULL,
                nights INTEGER NOT NULL,
                guests INTEGER NOT NULL,
                price_mode TEXT NOT NULL,
                price_per_night TEXT NOT NULL,
                currency TEXT NOT NULL,
                gross_foreign TEXT NOT NULL,
                czk_rate TEXT NOT NULL,
                gross_czk TEXT NOT NULL,
                dph_rate TEXT NOT NULL,
                dph_amount TEXT NOT NULL,
                revenue_without_dph TEXT NOT NULL,
                card_type TEXT NOT NULL,
                card_fee_rate TEXT NOT NULL,
                card_fee TEXT NOT NULL,
                variable_costs TEXT NOT NULL,
                net_profit TEXT NOT NULL,
                rates_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


def parse_decimal(raw: str, field_name: str) -> Decimal:
    normalized = raw.strip().replace(",", ".")
    try:
        value = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name}: enter a number") from exc
    if value < 0:
        raise ValueError(f"{field_name}: value cannot be negative")
    return value


def parse_int(raw: str, field_name: str) -> int:
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{field_name}: enter a whole number") from exc
    if value < 0:
        raise ValueError(f"{field_name}: value cannot be negative")
    return value


def parse_booking_date(raw: str) -> str:
    if not raw.strip():
        return today_iso()
    try:
        return date.fromisoformat(raw.strip()).isoformat()
    except ValueError as exc:
        raise ValueError("Booking date: use YYYY-MM-DD") from exc


def one_value(query: dict[str, list[str]], name: str, default: str = "") -> str:
    return query.get(name, [default])[0]


def decimal_from_saved(raw: Any) -> Decimal:
    return Decimal(str(raw or "0"))


def row_to_booking(row: sqlite3.Row) -> dict[str, Any]:
    booking = {column: row[column] for column in BOOKING_COLUMNS}
    booking["nights"] = int(booking["nights"])
    booking["guests"] = int(booking["guests"])
    return booking


def fetch_cnb_rates() -> tuple[dict[str, CurrencyRate], str]:
    request = urllib.request.Request(
        CNB_DAILY_RATES_URL,
        headers={"User-Agent": "Local profit calculator/1.0"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        text = response.read().decode("utf-8")

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 3:
        raise RuntimeError("CNB returned an empty exchange-rate table")

    table_date = lines[0]
    rates: dict[str, CurrencyRate] = {
        "CZK": CurrencyRate("CZK", Decimal("1"), Decimal("1"), "Czech koruna")
    }
    for line in lines[2:]:
        country, currency, amount, code, rate = line.split("|")
        rates[code.upper()] = CurrencyRate(
            code=code.upper(),
            amount=Decimal(amount),
            rate_czk=Decimal(rate.replace(",", ".")),
            name=f"{country} {currency}",
        )
    return rates, table_date


def calculate_profit(query: dict[str, list[str]]) -> dict[str, Any]:
    nights = parse_int(one_value(query, "nights"), "Nights")
    guests = parse_int(one_value(query, "guests"), "Guests")
    price_per_night = parse_decimal(one_value(query, "price_per_night"), "Price per night")
    variable_costs = parse_decimal(one_value(query, "variable_costs", "0"), "Additional costs")
    dph_rate = parse_decimal(one_value(query, "dph_rate", str(DEFAULT_DPH_RATE)), "DPH")
    card_fee_rate = parse_decimal(one_value(query, "card_fee_rate", ""), "Card fee")
    currency = one_value(query, "currency", "CZK").upper()
    price_mode = one_value(query, "price_mode", "booking")

    rates, rates_date = fetch_cnb_rates()
    if currency not in rates:
        supported = ", ".join(sorted(rates))
        raise ValueError(f"Currency {currency} was not found in CNB rates. Available: {supported}")

    multiplier = guests if price_mode == "guest" else 1
    gross_foreign = Decimal(nights) * Decimal(multiplier) * price_per_night
    czk_rate = rates[currency].czk_per_unit
    gross_czk = gross_foreign * czk_rate

    dph_amount = gross_czk * dph_rate / (Decimal("100") + dph_rate)
    revenue_without_dph = gross_czk - dph_amount
    card_fee = percent(gross_czk, card_fee_rate)
    net_profit = revenue_without_dph - card_fee - variable_costs

    return {
        "nights": nights,
        "guests": guests,
        "price_per_night": money(price_per_night),
        "price_mode": price_mode,
        "currency": currency,
        "rates_date": rates_date,
        "czk_rate": money(czk_rate),
        "gross_foreign": money(gross_foreign),
        "gross_czk": money(gross_czk),
        "dph_rate": money(dph_rate),
        "dph_amount": money(dph_amount),
        "revenue_without_dph": money(revenue_without_dph),
        "card_fee_rate": money(card_fee_rate),
        "card_fee": money(card_fee),
        "variable_costs": money(variable_costs),
        "net_profit": money(net_profit),
    }


def save_booking(query: dict[str, list[str]]) -> dict[str, Any]:
    result = calculate_profit(query)
    booking_date = parse_booking_date(one_value(query, "booking_date", today_iso()))
    guest_name = one_value(query, "guest_name", "Guest").strip() or "Guest"
    property_name = one_value(query, "property_name", "Property").strip() or "Property"
    card_type = one_value(query, "card_type", "Visa").strip() or "Visa"

    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO bookings (
                booking_date, guest_name, property_name, nights, guests, price_mode,
                price_per_night, currency, gross_foreign, czk_rate, gross_czk,
                dph_rate, dph_amount, revenue_without_dph, card_type, card_fee_rate,
                card_fee, variable_costs, net_profit, rates_date, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                booking_date,
                guest_name,
                property_name,
                result["nights"],
                result["guests"],
                result["price_mode"],
                result["price_per_night"],
                result["currency"],
                result["gross_foreign"],
                result["czk_rate"],
                result["gross_czk"],
                result["dph_rate"],
                result["dph_amount"],
                result["revenue_without_dph"],
                card_type,
                result["card_fee_rate"],
                result["card_fee"],
                result["variable_costs"],
                result["net_profit"],
                result["rates_date"],
                now_iso(),
            ),
        )
        booking_id = cursor.lastrowid

    return {"id": booking_id, **result}


def list_bookings(month: str = "") -> dict[str, Any]:
    where = ""
    params: tuple[str, ...] = ()
    if month:
        if len(month) != 7:
            raise ValueError("Month: use YYYY-MM")
        where = "WHERE substr(booking_date, 1, 7) = ?"
        params = (month,)

    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(BOOKING_COLUMNS)} FROM bookings {where} ORDER BY booking_date DESC, id DESC",
            params,
        ).fetchall()

    bookings = [row_to_booking(row) for row in rows]
    totals = {
        "bookings": len(bookings),
        "nights": sum(booking["nights"] for booking in bookings),
        "guests": sum(booking["guests"] for booking in bookings),
        "gross_czk": money(sum((decimal_from_saved(booking["gross_czk"]) for booking in bookings), Decimal("0"))),
        "dph_amount": money(sum((decimal_from_saved(booking["dph_amount"]) for booking in bookings), Decimal("0"))),
        "card_fee": money(sum((decimal_from_saved(booking["card_fee"]) for booking in bookings), Decimal("0"))),
        "variable_costs": money(sum((decimal_from_saved(booking["variable_costs"]) for booking in bookings), Decimal("0"))),
        "net_profit": money(sum((decimal_from_saved(booking["net_profit"]) for booking in bookings), Decimal("0"))),
    }
    return {"bookings": bookings, "totals": totals}


def delete_booking(query: dict[str, list[str]]) -> dict[str, Any]:
    booking_id = parse_int(one_value(query, "id"), "Booking ID")
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
    return {"deleted": cursor.rowcount}


def build_currency_options() -> str:
    fallback = ["CZK", "EUR", "USD", "GBP", "PLN", "HUF"]
    try:
        rates, _ = fetch_cnb_rates()
        codes = sorted(rates)
    except (RuntimeError, urllib.error.URLError, TimeoutError, ValueError):
        codes = fallback
    return "\n".join(
        f'<option value="{code}" {"selected" if code == "EUR" else ""}>{code}</option>'
        for code in codes
    )


def page_html() -> bytes:
    options = build_currency_options()
    visa_fee = money(DEFAULT_CARD_FEES["visa"])
    mastercard_fee = money(DEFAULT_CARD_FEES["mastercard"])
    cash_fee = money(DEFAULT_CARD_FEES["cash"])
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Accommodation Profit Calculator</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f7f9;
      --panel: #ffffff;
      --line: #d9e0e7;
      --text: #1d252d;
      --muted: #64717f;
      --accent: #0f766e;
      --accent-dark: #115e59;
      --danger: #b42318;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Arial, Helvetica, sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    main {{
      width: min(1120px, calc(100% - 32px));
      margin: 28px auto;
    }}
    h1 {{
      margin: 0 0 18px;
      font-size: clamp(26px, 3vw, 38px);
      line-height: 1.15;
    }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(320px, 460px) 1fr;
      gap: 18px;
      align-items: start;
    }}
    form, .result {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }}
    label {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }}
    input, select {{
      width: 100%;
      min-height: 42px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px 10px;
      font: inherit;
      color: var(--text);
      background: #fff;
    }}
    fieldset {{
      border: 1px solid var(--line);
      border-radius: 8px;
      margin: 16px 0 0;
      padding: 12px;
    }}
    legend {{
      padding: 0 6px;
      color: var(--muted);
      font-weight: 700;
      font-size: 13px;
    }}
    .segmented {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }}
    .segmented label {{
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      cursor: pointer;
      color: var(--text);
      font-weight: 700;
    }}
    .segmented input {{
      position: absolute;
      opacity: 0;
      pointer-events: none;
    }}
    .segmented label:has(input:checked) {{
      border-color: var(--accent);
      background: #e6f3f1;
      color: var(--accent-dark);
    }}
    button {{
      width: 100%;
      min-height: 44px;
      margin-top: 16px;
      border: 0;
      border-radius: 6px;
      background: var(--accent);
      color: #fff;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }}
    button:hover {{ background: var(--accent-dark); }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .metric {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 13px;
      min-height: 92px;
      background: #fbfcfd;
    }}
    .metric span {{
      display: block;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
      margin-bottom: 8px;
    }}
    .metric strong {{
      display: block;
      font-size: 24px;
      line-height: 1.1;
      overflow-wrap: anywhere;
    }}
    .wide {{ grid-column: 1 / -1; }}
    .formula {{
      margin-top: 14px;
      color: var(--muted);
      line-height: 1.45;
      font-size: 14px;
    }}
    .toolbar {{
      display: flex;
      gap: 10px;
      align-items: end;
      margin-bottom: 14px;
      flex-wrap: wrap;
    }}
    .toolbar label {{
      min-width: 180px;
    }}
    .secondary {{
      width: auto;
      min-width: 120px;
      margin-top: 0;
      background: #334155;
    }}
    .secondary:hover {{ background: #1f2937; }}
    .danger {{
      width: auto;
      min-height: 32px;
      margin-top: 0;
      padding: 6px 10px;
      background: #b42318;
      font-size: 13px;
    }}
    .danger:hover {{ background: #8a1f15; }}
    .table-wrap {{
      width: 100%;
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 880px;
      font-size: 14px;
    }}
    th, td {{
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      white-space: nowrap;
    }}
    th {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .03em;
      background: #f8fafc;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    .section-title {{
      margin: 28px 0 12px;
      font-size: 22px;
    }}
    .error {{
      color: var(--danger);
      font-weight: 700;
      min-height: 20px;
      margin-top: 12px;
    }}
    @media (max-width: 760px) {{
      main {{ width: min(100% - 20px, 560px); margin-top: 16px; }}
      .layout, .grid, .summary {{ grid-template-columns: 1fr; }}
      .wide {{ grid-column: auto; }}
      .toolbar {{ display: grid; }}
      .secondary {{ width: 100%; }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>Accommodation Net Profit Calculator</h1>
    <div class="layout">
      <form id="calculator">
        <div class="grid">
          <label>Nights
            <input name="nights" type="number" min="0" step="1" value="3" required>
          </label>
          <label>Guests
            <input name="guests" type="number" min="0" step="1" value="2" required>
          </label>
          <label>Price per night
            <input name="price_per_night" type="number" min="0" step="0.01" value="90" required>
          </label>
          <label>Payment currency
            <select name="currency">{options}</select>
          </label>
          <label>DPH, %
            <input name="dph_rate" type="number" min="0" step="0.01" value="{money(DEFAULT_DPH_RATE)}" required>
          </label>
          <label>Additional costs, CZK
            <input name="variable_costs" type="number" min="0" step="0.01" value="0" required>
          </label>
          <label>Booking date
            <input name="booking_date" type="date" id="bookingDate" required>
          </label>
          <label>Guest name
            <input name="guest_name" type="text" value="Guest">
          </label>
          <label class="wide">Property
            <input name="property_name" type="text" value="Main apartment">
          </label>
        </div>

        <fieldset>
          <legend>Price basis</legend>
          <div class="segmented">
            <label><input type="radio" name="price_mode" value="booking" checked>Per booking</label>
            <label><input type="radio" name="price_mode" value="guest">Per guest</label>
          </div>
        </fieldset>

        <fieldset>
          <legend>Payment method</legend>
          <div class="grid">
            <label>Card
              <select id="cardType" name="card_type">
                <option data-fee="{visa_fee}" value="Visa" selected>Visa</option>
                <option data-fee="{mastercard_fee}" value="Mastercard">Mastercard</option>
                <option data-fee="{cash_fee}" value="Cash/bank transfer">Cash/bank transfer</option>
                <option data-fee="custom" value="Custom">Custom rate</option>
              </select>
            </label>
            <label>Card fee, %
              <input id="cardFee" name="card_fee_rate" type="number" min="0" step="0.01" value="{visa_fee}" required>
            </label>
          </div>
        </fieldset>

        <button type="submit">Calculate</button>
        <button type="button" id="saveBooking" class="secondary">Save booking</button>
        <div class="error" id="error"></div>
      </form>

      <section class="result">
        <div class="summary" id="summary">
          <div class="metric wide"><span>Net profit</span><strong id="net">-</strong></div>
          <div class="metric"><span>Payment total</span><strong id="grossForeign">-</strong></div>
          <div class="metric"><span>Total in CZK</span><strong id="grossCzk">-</strong></div>
          <div class="metric"><span>DPH included</span><strong id="dph">-</strong></div>
          <div class="metric"><span>Card fee</span><strong id="card">-</strong></div>
          <div class="metric"><span>Revenue without DPH</span><strong id="withoutDph">-</strong></div>
          <div class="metric"><span>Exchange rate to CZK</span><strong id="rate">-</strong></div>
        </div>
        <p class="formula" id="formula">
          Formula: payment total -> CZK, then DPH is extracted from the gross
          amount, and Visa/Mastercard fees plus additional costs are deducted.
        </p>
      </section>
    </div>

    <h2 class="section-title">Mini Accounting</h2>
    <section class="result">
      <div class="toolbar">
        <label>Month
          <input id="monthFilter" type="month">
        </label>
        <button type="button" id="loadBookings" class="secondary">Load</button>
      </div>
      <div class="summary">
        <div class="metric"><span>Bookings</span><strong id="totalBookings">0</strong></div>
        <div class="metric"><span>Nights</span><strong id="totalNights">0</strong></div>
        <div class="metric"><span>Gross revenue</span><strong id="totalGross">0.00 CZK</strong></div>
        <div class="metric"><span>DPH</span><strong id="totalDph">0.00 CZK</strong></div>
        <div class="metric"><span>Card fees</span><strong id="totalCard">0.00 CZK</strong></div>
        <div class="metric"><span>Net profit</span><strong id="totalNet">0.00 CZK</strong></div>
      </div>
      <div class="table-wrap" style="margin-top: 14px;">
        <table>
          <thead>
            <tr>
              <th>Date</th>
              <th>Guest</th>
              <th>Property</th>
              <th>Nights</th>
              <th>Payment</th>
              <th>Gross CZK</th>
              <th>DPH</th>
              <th>Fees</th>
              <th>Net</th>
              <th></th>
            </tr>
          </thead>
          <tbody id="bookingRows">
            <tr><td colspan="10">No bookings saved yet.</td></tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>

  <script>
    const form = document.querySelector("#calculator");
    const error = document.querySelector("#error");
    const cardType = document.querySelector("#cardType");
    const cardFee = document.querySelector("#cardFee");
    const saveBooking = document.querySelector("#saveBooking");
    const loadBookings = document.querySelector("#loadBookings");
    const monthFilter = document.querySelector("#monthFilter");
    const bookingRows = document.querySelector("#bookingRows");
    let lastCalculation = null;

    cardType.addEventListener("change", () => {{
      const fee = cardType.selectedOptions[0].dataset.fee;
      if (fee !== "custom") cardFee.value = fee;
      cardFee.readOnly = fee !== "custom";
    }});
    cardFee.readOnly = true;
    document.querySelector("#bookingDate").valueAsDate = new Date();
    monthFilter.value = new Date().toISOString().slice(0, 7);

    function setText(id, text) {{
      document.querySelector(id).textContent = text;
    }}

    async function calculate() {{
      error.textContent = "";
      const params = new URLSearchParams(new FormData(form));
      const response = await fetch(`/api/calculate?${{params.toString()}}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Calculation error");
      lastCalculation = data;

      setText("#net", `${{data.net_profit}} CZK`);
      setText("#grossForeign", `${{data.gross_foreign}} ${{data.currency}}`);
      setText("#grossCzk", `${{data.gross_czk}} CZK`);
      setText("#dph", `${{data.dph_amount}} CZK`);
      setText("#card", `${{data.card_fee}} CZK`);
      setText("#withoutDph", `${{data.revenue_without_dph}} CZK`);
      setText("#rate", `1 ${{data.currency}} = ${{data.czk_rate}} CZK`);
      document.querySelector("#formula").textContent =
        `CNB exchange rate: ${{data.rates_date}}. DPH ${{data.dph_rate}}%, card fee ${{data.card_fee_rate}}%.`;
      return data;
    }}

    async function loadAccounting() {{
      const params = new URLSearchParams();
      if (monthFilter.value) params.set("month", monthFilter.value);
      const response = await fetch(`/api/bookings?${{params.toString()}}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Could not load bookings");

      setText("#totalBookings", data.totals.bookings);
      setText("#totalNights", data.totals.nights);
      setText("#totalGross", `${{data.totals.gross_czk}} CZK`);
      setText("#totalDph", `${{data.totals.dph_amount}} CZK`);
      setText("#totalCard", `${{data.totals.card_fee}} CZK`);
      setText("#totalNet", `${{data.totals.net_profit}} CZK`);

      if (!data.bookings.length) {{
        bookingRows.innerHTML = '<tr><td colspan="10">No bookings saved yet.</td></tr>';
        return;
      }}

      bookingRows.innerHTML = data.bookings.map((booking) => `
        <tr>
          <td>${{booking.booking_date}}</td>
          <td>${{booking.guest_name}}</td>
          <td>${{booking.property_name}}</td>
          <td>${{booking.nights}}</td>
          <td>${{booking.gross_foreign}} ${{booking.currency}}</td>
          <td>${{booking.gross_czk}}</td>
          <td>${{booking.dph_amount}}</td>
          <td>${{booking.card_fee}}</td>
          <td><strong>${{booking.net_profit}}</strong></td>
          <td><button class="danger" type="button" data-delete="${{booking.id}}">Delete</button></td>
        </tr>
      `).join("");
    }}

    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      try {{
        await calculate();
      }} catch (err) {{
        error.textContent = err.message;
      }}
    }});

    saveBooking.addEventListener("click", async () => {{
      try {{
        await calculate();
        const params = new URLSearchParams(new FormData(form));
        const response = await fetch(`/api/bookings/save?${{params.toString()}}`);
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Could not save booking");
        error.textContent = `Saved booking #${{data.id}}`;
        await loadAccounting();
      }} catch (err) {{
        error.textContent = err.message;
      }}
    }});

    loadBookings.addEventListener("click", async () => {{
      try {{
        await loadAccounting();
      }} catch (err) {{
        error.textContent = err.message;
      }}
    }});

    bookingRows.addEventListener("click", async (event) => {{
      const button = event.target.closest("[data-delete]");
      if (!button) return;
      const response = await fetch(`/api/bookings/delete?id=${{button.dataset.delete}}`);
      const data = await response.json();
      if (!response.ok) {{
        error.textContent = data.error || "Could not delete booking";
        return;
      }}
      await loadAccounting();
    }});

    calculate().catch((err) => error.textContent = err.message);
    loadAccounting().catch((err) => error.textContent = err.message);
  </script>
</body>
</html>"""
    return html.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page_html())
            return

        if parsed.path == "/api/calculate":
            try:
                result = calculate_profit(parse_qs(parsed.query))
                payload = json.dumps(result).encode("utf-8")
                status = 200
            except (ValueError, RuntimeError, urllib.error.URLError, TimeoutError) as exc:
                payload = json.dumps({"error": str(exc)}).encode("utf-8")
                status = 400

            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(payload)
            return

        if parsed.path == "/api/bookings":
            try:
                query = parse_qs(parsed.query)
                result = list_bookings(one_value(query, "month", ""))
                payload = json.dumps(result).encode("utf-8")
                status = 200
            except ValueError as exc:
                payload = json.dumps({"error": str(exc)}).encode("utf-8")
                status = 400

            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(payload)
            return

        if parsed.path == "/api/bookings/save":
            try:
                result = save_booking(parse_qs(parsed.query))
                payload = json.dumps(result).encode("utf-8")
                status = 200
            except (ValueError, RuntimeError, urllib.error.URLError, TimeoutError) as exc:
                payload = json.dumps({"error": str(exc)}).encode("utf-8")
                status = 400

            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(payload)
            return

        if parsed.path == "/api/bookings/delete":
            try:
                result = delete_booking(parse_qs(parsed.query))
                payload = json.dumps(result).encode("utf-8")
                status = 200
            except ValueError as exc:
                payload = json.dumps({"error": str(exc)}).encode("utf-8")
                status = 400

            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Not found".encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), format % args))


def main() -> None:
    init_db()
    server = ThreadingHTTPServer(("127.0.0.1", 8000), Handler)
    print("Open in your browser: http://127.0.0.1:8000")
    server.serve_forever()


if __name__ == "__main__":
    main()

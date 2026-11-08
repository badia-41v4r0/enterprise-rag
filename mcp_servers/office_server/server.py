"""Demo "third-party" MCP server: ACME office information (holidays, offices, currency conversion).

It plays the role of an external MCP that knows nothing about EnterpriseRAG: no permission context,
no classification labels. It is registered through the admin UI (R4) and can run in the isolated
runner (R5) as an approved image, where it has no network at all and is reached over a Unix socket.
Data is static and synthetic. If ERAG_OFFICE_API_TOKEN is set (a secret assigned by the admin), the
`office_info` tool reports that it is configured, without revealing it.
"""

from __future__ import annotations

import os
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("acme-office", instructions="ACME office information: public holidays, offices, currency rates.")

HOLIDAYS = {
    "ES": [
        ("2026-01-01", "New Year"),
        ("2026-01-06", "Epiphany"),
        ("2026-05-01", "Labour Day"),
        ("2026-10-12", "National Day"),
        ("2026-12-08", "Immaculate Conception"),
        ("2026-12-25", "Christmas"),
    ],
    "PT": [
        ("2026-01-01", "New Year"),
        ("2026-04-25", "Freedom Day"),
        ("2026-06-10", "Portugal Day"),
        ("2026-12-25", "Christmas"),
    ],
    "DE": [
        ("2026-01-01", "New Year"),
        ("2026-05-01", "Labour Day"),
        ("2026-10-03", "German Unity Day"),
        ("2026-12-25", "Christmas"),
        ("2026-12-26", "Boxing Day"),
    ],
}
OFFICES = {
    "madrid": {"address": "Calle Ficticia 12, Madrid", "desks": 120, "opening_hours": "08:00-20:00"},
    "lisbon": {"address": "Rua Imaginaria 7, Lisbon", "desks": 60, "opening_hours": "08:30-19:00"},
    "berlin": {"address": "Beispielstrasse 3, Berlin", "desks": 45, "opening_hours": "08:00-19:00"},
}
EUR_RATES = {"EUR": 1.0, "USD": 1.08, "GBP": 0.85, "CHF": 0.95}


@mcp.tool()
def list_holidays(country: Literal["ES", "PT", "DE"]) -> dict[str, Any]:
    """Public holidays in 2026 for a country where ACME has an office."""
    return {"country": country, "holidays": [{"date": d, "name": n} for d, n in HOLIDAYS[country]]}


@mcp.tool()
def office_info(city: Literal["madrid", "lisbon", "berlin"]) -> dict[str, Any]:
    """Address, desk count and opening hours of an ACME office."""
    info = dict(OFFICES[city])
    info["booking_system"] = "configured" if os.environ.get("ERAG_OFFICE_API_TOKEN") else "not configured"
    return {"city": city, **info}


@mcp.tool()
def convert_currency(
    amount: float, from_currency: Literal["EUR", "USD", "GBP", "CHF"], to_currency: Literal["EUR", "USD", "GBP", "CHF"]
) -> dict[str, Any]:
    """Convert an amount between currencies with ACME's fixed internal rates for 2026."""
    value = amount / EUR_RATES[from_currency] * EUR_RATES[to_currency]
    return {"amount": amount, "from": from_currency, "to": to_currency, "result": round(value, 2)}


def main() -> None:
    from mcp_servers.serve import serve

    serve(mcp)

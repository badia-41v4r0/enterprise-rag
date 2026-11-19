"""The evaluation corpus: ACME plus a generated, deterministic set of documents that stress retrieval.

Run `python -m app.eval.corpus` (or `erag eval generate`) to rewrite `eval/corpus/acme-eval/` and
`eval/sets/acme.generated.yaml`. The output is committed; a test checks that regenerating changes nothing.

Everything here is fictional. The structure extends the ACME demo (demo/acme/seed.py) and is only ever seeded
into the evaluation database.
"""

from __future__ import annotations

import dataclasses
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.core.config import REPO_ROOT
from app.models import Action, Classification
from demo.acme.seed import CONTAINERS, PERSONAS, ROLE_DOCUMENTS, ROLES, ContainerSpec, Persona

EVAL_DIR = REPO_ROOT / "eval"
CORPUS_DIR = EVAL_DIR / "corpus" / "acme-eval"
SETS_DIR = EVAL_DIR / "sets"
ACME_DOCS_DIR = REPO_ROOT / "demo" / "acme" / "docs"
SEED = 20261118

# --- structure ---------------------------------------------------------------------------------------------

_HR_EXTRA_RESTRICTED = {
    "investigation-2026-07.md": "CANARY-HRR-8PL2",
    "settlement-2026-05.md": "CANARY-HRR-3VN7",
}


def _containers() -> list[ContainerSpec]:
    out = []
    for c in CONTAINERS:
        if c.slug == "hr-confidential":
            c = dataclasses.replace(c, restricted_docs={**c.restricted_docs, **_HR_EXTRA_RESTRICTED})
        out.append(c)
    out += [
        ContainerSpec(
            "sales-contracts",
            "Sales Contracts",
            Classification.confidential,
            "Customer contracts (evaluation corpus)",
            "CANARY-SAL-4RJ9",
        ),
        ContainerSpec(
            "support-kb",
            "Support Knowledge Base",
            Classification.internal,
            "Error codes and fixes (evaluation corpus)",
            "CANARY-SUP-6HW2",
        ),
        ContainerSpec(
            "product-catalog",
            "Product Catalog",
            Classification.public,
            "Hardware product sheets (evaluation corpus)",
            "CANARY-CAT-1MZ5",
        ),
    ]
    return out


EVAL_CONTAINERS: list[ContainerSpec] = _containers()

EVAL_ROLES: dict[str, dict[str, Action]] = {
    **{name: dict(grants) for name, grants in ROLES.items()},
    "Sales": {"sales-contracts": Action.manage},
    "Support": {"support-kb": Action.read},
}
EVAL_ROLES["Employee"]["product-catalog"] = Action.read
EVAL_ROLES["Executive"] = {c.slug: Action.read for c in EVAL_CONTAINERS}

EVAL_ROLE_DOCUMENTS: dict[str, list[str]] = {
    **{k: list(v) for k, v in ROLE_DOCUMENTS.items()},
}
EVAL_ROLE_DOCUMENTS["HR"] = [*EVAL_ROLE_DOCUMENTS.get("HR", []), *_HR_EXTRA_RESTRICTED]

EVAL_PERSONAS: list[Persona] = [
    *PERSONAS,
    Persona("sales@acme.test", "Sara Sales Manager", ["Employee", "Sales"]),
    Persona("support@acme.test", "Sergio Support Agent", ["Employee", "Support"]),
]

DOC_DIRS: list[Path] = [ACME_DOCS_DIR, CORPUS_DIR]


@dataclass(frozen=True)
class Oracle:
    """Who may read what, computed from the declared structure only (never from app.authz)."""

    def persona(self, email: str) -> Persona:
        for p in EVAL_PERSONAS:
            if p.email == email:
                return p
        raise KeyError(email)

    def can_read(self, email: str, doc_key: str) -> bool:
        p = self.persona(email)
        slug, _, filename = doc_key.partition("/")
        if not any(slug in EVAL_ROLES[r] for r in p.roles):
            return False
        spec = next(c for c in EVAL_CONTAINERS if c.slug == slug)
        if filename in spec.restricted_docs:
            return any(filename in EVAL_ROLE_DOCUMENTS.get(r, []) for r in p.roles)
        return True


def document_keys() -> set[str]:
    """Every `container/filename` the evaluation database will hold."""
    keys = set()
    for spec in EVAL_CONTAINERS:
        for d in DOC_DIRS:
            keys |= {f"{spec.slug}/{p.name}" for p in (d / spec.slug).glob("*.md")}
    return keys


# --- generated documents ----------------------------------------------------------------------------------

_CLIENT_A = [
    "Iberdata",
    "Albar",
    "Norvento",
    "Castellana",
    "Mediterránea",
    "Atlántica",
    "Solaris",
    "Ebro",
    "Guadiana",
    "Cierzo",
    "Montsec",
    "Tajo",
    "Sierra",
    "Marítima",
    "Levante",
    "Duero",
    "Cantábrica",
    "Pirineo",
    "Bética",
    "Alhambra",
]
_CLIENT_B = ["Soluciones", "Logística", "Energía", "Construcciones", "Distribución", "Servicios", "Ingeniería"]
_CLIENT_C = ["S.L.", "S.A."]
_SERVICES = [
    ("mantenimiento preventivo de la flota de sensores", "mantenimiento"),
    ("licencias de ACME Cloud Planner Business", "licencias"),
    ("consultoría de despliegue e integración", "consultoría"),
    ("soporte técnico 24x7", "soporte"),
    ("migración a la nube de la plataforma de planificación", "migración"),
]
_TERMS = [30, 45, 60, 90]
_DURATIONS = [12, 18, 24, 36]
_MONTHS_ES = [
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
]
_CONTACTS = [
    "Lucía Ferrer",
    "Javier Ortega",
    "Marta Ibáñez",
    "Andrés Molina",
    "Carmen Vidal",
    "Raúl Serrano",
    "Elena Campos",
    "Pablo Nieto",
]

_COMPONENTS = [
    ("AX Gateway", "gateway"),
    ("AX Sync", "sync"),
    ("Portal de clientes", "portal"),
    ("API pública", "api"),
    ("App móvil", "mobile"),
]
_ERRORS = [
    # (Spanish title, English title, cause es, cause en, fix es, fix en)
    (
        "Certificado TLS caducado",
        "Expired TLS certificate",
        "el certificado TLS del dispositivo ha caducado y el servidor rechaza la conexión",
        "the device TLS certificate has expired and the server rejects the connection",
        "renovar el certificado desde la consola de dispositivos y reiniciar el servicio",
        "renew the certificate from the device console and restart the service",
    ),
    (
        "Token de API revocado",
        "Revoked API token",
        "el token de API usado por la integración fue revocado por un administrador",
        "the API token used by the integration was revoked by an administrator",
        "generar un token nuevo en Ajustes > API y actualizar la integración",
        "create a new token in Settings > API and update the integration",
    ),
    (
        "Cuota de sincronización superada",
        "Sync quota exceeded",
        "la cuenta ha superado el número de sincronizaciones por hora de su plan",
        "the account exceeded the hourly sync quota of its plan",
        "esperar a la siguiente hora o ampliar el plan a Business",
        "wait for the next hour or upgrade the plan to Business",
    ),
    (
        "Reloj del dispositivo desincronizado",
        "Device clock out of sync",
        "el reloj del dispositivo difiere más de cinco minutos del servidor NTP",
        "the device clock differs by more than five minutes from the NTP server",
        "activar NTP en el dispositivo y comprobar el puerto UDP 123",
        "enable NTP on the device and check UDP port 123",
    ),
    (
        "Fallo de autenticación MFA",
        "MFA authentication failure",
        "el segundo factor (MFA) no coincide o el código ha caducado",
        "the second factor (MFA) does not match or the code expired",
        "sincronizar la hora del móvil y volver a introducir el código",
        "sync the phone clock and enter the code again",
    ),
    (
        "VPN sin ruta al servidor",
        "VPN has no route to the server",
        "el perfil de VPN no incluye la subred del servidor de sincronización",
        "the VPN profile does not include the sync server subnet",
        "solicitar el perfil eng-vpn actualizado en el portal de IT",
        "request the updated eng-vpn profile in the IT portal",
    ),
]

_FAMILIES = [
    ("sensor", "Temperature and humidity sensor"),
    ("gateway", "Edge gateway"),
    ("controller", "Field controller"),
    ("relay", "Smart relay"),
]
_IP = ["IP54", "IP65", "IP67"]
_VENDORS = [
    "Nimbus Hosting BV",
    "Kestrel Logistics Ltd",
    "Arcadia Office Supplies",
    "Quanta Telecom SA",
    "Helios Facilities GmbH",
]


@dataclass
class GeneratedDoc:
    slug: str
    filename: str
    text: str

    @property
    def key(self) -> str:
        return f"{self.slug}/{self.filename}"


@dataclass
class Generated:
    docs: list[GeneratedDoc]
    questions: list[dict[str, Any]]


def _eur(n: int) -> str:
    return f"{n:,}".replace(",", ".") + " EUR"


def _contracts(rng: random.Random) -> tuple[list[GeneratedDoc], list[dict[str, Any]]]:
    docs, questions = [], []
    clients = [f"{a} {b} {c}" for a in _CLIENT_A for b in _CLIENT_B[:2] for c in _CLIENT_C[:1]]
    rng.shuffle(clients)
    for i in range(1, 81):
        cid = f"CT-2025-{i:04d}"
        client = clients[(i - 1) % 40]  # 40 clients, two contracts each (near-duplicates)
        service, _short = _SERVICES[rng.randrange(len(_SERVICES))]
        term = rng.choice(_TERMS)
        duration = rng.choice(_DURATIONS)
        amount = rng.randrange(12, 240) * 500
        month = rng.randrange(12)
        penalty = rng.choice([2, 3, 5])
        contact = rng.choice(_CONTACTS)
        filename = f"{cid.lower()}.md"
        text = f"""# Contrato {cid}: {client}

**Cliente:** {client}
**Servicio:** {service}
**Fecha de inicio:** 1 de {_MONTHS_ES[month]} de 2025
**Duración:** {duration} meses
**Responsable de cuenta:** {contact}

## Condiciones económicas

El importe total del contrato {cid} es de {_eur(amount)}, facturado mensualmente a partes iguales.
El plazo de pago es de {term} días desde la fecha de cada factura. Los retrasos devengan el interés legal.

## Nivel de servicio

ACME se compromete a una disponibilidad del 99,5 % mensual y a responder a las incidencias críticas en menos de
cuatro horas laborables. Las incidencias se comunican por el portal de clientes.

## Penalizaciones y renovación

Si la disponibilidad mensual baja del compromiso, el cliente recibe un descuento del {penalty} % sobre la factura
del mes. El contrato se renueva automáticamente por periodos de 12 meses salvo preaviso de 60 días.
"""
        docs.append(GeneratedDoc("sales-contracts", filename, text))
        key = f"sales-contracts/{filename}"
        if i % 2 == 0:
            q, tag = f"¿Cuál es el plazo de pago del contrato {cid}?", "payment-term"
        elif i % 3 == 0:
            q, tag = f"¿Cuánto cuesta en total el contrato {cid}?", "amount"
        else:
            q, tag = f"¿Qué duración tiene el contrato {cid} y con qué cliente es?", "duration"
        if i <= 40:
            questions.append(
                {
                    "id": f"gen-ct-{i:03d}",
                    "question": q,
                    "as_users": ["sales@acme.test", "newhire@acme.test"],
                    "expect": {
                        "sales@acme.test": {"documents": [key]},
                        "newhire@acme.test": {"answerable": False, "must_not_see": [key]},
                    },
                    "tags": ["generated", "exact-id", "es", tag],
                }
            )
    return docs, questions


def _support(rng: random.Random) -> tuple[list[GeneratedDoc], list[dict[str, Any]]]:
    docs, questions = [], []
    for i in range(60):
        code = f"ERR-{401 + i}"
        title_es, title_en, cause_es, cause_en, fix_es, fix_en = _ERRORS[i % len(_ERRORS)]
        comp, comp_short = _COMPONENTS[rng.randrange(len(_COMPONENTS))]
        since = rng.choice(["4.0", "4.1", "4.2"])
        filename = f"{code.lower()}.md"
        if i % 2 == 0:
            text = f"""# {code}: {title_es} ({comp})

**Componente:** {comp}
**Desde la versión:** {since}

## Síntoma

El componente {comp} muestra el código {code} y deja de sincronizar con ACME Cloud Planner.

## Causa

En la mayoría de los casos {cause_es}.

## Solución

Para resolverlo hay que {fix_es}. Si el error persiste después de 30 minutos, abrir un ticket de nivel 2
indicando el código {code} y el número de serie del equipo.
"""
            lang = "es"
        else:
            text = f"""# {code}: {title_en} ({comp})

**Component:** {comp}
**Since version:** {since}

## Symptom

{comp} reports error {code} and stops syncing with ACME Cloud Planner.

## Cause

In most cases {cause_en}.

## Fix

To resolve it, {fix_en}. If the error persists after 30 minutes, open a level 2 ticket with the code {code}
and the serial number of the device.
"""
            lang = "en"
        docs.append(GeneratedDoc("support-kb", filename, text))
        key = f"support-kb/{filename}"
        if i < 30:
            q = (
                f"¿Cómo se soluciona el error {code}?"
                if rng.random() < 0.5
                else f"What does error {code} mean and how do I fix it?"
            )
            questions.append(
                {
                    "id": f"gen-err-{i + 1:03d}",
                    "question": q,
                    "as_users": ["support@acme.test", "newhire@acme.test"],
                    "expect": {
                        "support@acme.test": {"documents": [key]},
                        "newhire@acme.test": {"answerable": False, "must_not_see": [key]},
                    },
                    "tags": ["generated", "exact-id", lang, comp_short],
                }
            )
    return docs, questions


def _catalog(rng: random.Random) -> tuple[list[GeneratedDoc], list[dict[str, Any]]]:
    docs, questions = [], []
    for i in range(80):
        sku = f"AX-{1001 + i}"
        fam, fam_name = _FAMILIES[i % len(_FAMILIES)]
        weight = rng.randrange(80, 900, 5)
        power = rng.choice([1.5, 2.0, 3.5, 5.0, 7.5])
        ip = rng.choice(_IP)
        tmin, tmax = rng.choice([(-20, 60), (-30, 70), (-10, 50), (-40, 85)])
        price = rng.randrange(49, 690)
        filename = f"{sku.lower()}.md"
        text = f"""# {sku} {fam_name}

Product sheet for the {sku}, part of the ACME field hardware line for ACME Cloud Planner.

| Property | Value |
|---|---|
| Family | {fam} |
| Weight | {weight} g |
| Power consumption | {power} W |
| Ingress protection | {ip} |
| Operating temperature | {tmin} °C to {tmax} °C |
| List price | {price} EUR |

The {sku} connects to an AX Gateway over the local radio network and is configured from the mobile app.
Warranty: two years. Firmware updates are delivered over the air.
"""
        docs.append(GeneratedDoc("product-catalog", filename, text))
        key = f"product-catalog/{filename}"
        if i < 20:
            q = (
                f"How much does the {sku} weigh?"
                if i % 2 == 0
                else f"What is the operating temperature range of the {sku}?"
            )
            questions.append(
                {
                    "id": f"gen-ax-{i + 1:03d}",
                    "question": q,
                    "as_users": ["newhire@acme.test"],
                    "expect": {"newhire@acme.test": {"documents": [key]}},
                    "tags": ["generated", "exact-id", "en", fam],
                }
            )
    return docs, questions


def _invoices(rng: random.Random) -> tuple[list[GeneratedDoc], list[dict[str, Any]]]:
    docs, questions = [], []
    for i in range(30):
        inv = f"INV-2026-{101 + i:04d}"
        vendor = rng.choice(_VENDORS)
        amount = rng.randrange(800, 64000, 50)
        day = rng.randrange(1, 29)
        month = rng.choice(["July", "August", "September", "October"])
        po = f"PO-{rng.randrange(3000, 3999)}"
        status = rng.choice(["open", "paid", "on hold"])
        approval = (
            "CFO approval required"
            if amount > 50000
            else ("second approval by the finance controller" if amount > 10000 else "standard approval")
        )
        filename = f"{inv.lower()}.md"
        text = f"""# Invoice {inv} (Confidential)

Finance confidential reference: CANARY-FIN-3TB6.

- Vendor: {vendor}
- Purchase order: {po}
- Amount: {amount:,} EUR
- Due date: {day} {month} 2026
- Status: {status}
- Approval: {approval}

Payment follows the vendor payment policy (net-45 unless the contract says otherwise).
"""
        docs.append(GeneratedDoc("finance", filename, text))
        key = f"finance/{filename}"
        if i < 10:
            questions.append(
                {
                    "id": f"gen-inv-{i + 1:03d}",
                    "question": f"When is invoice {inv} due and what is its status?",
                    "as_users": ["finance@acme.test", "engineer@acme.test"],
                    "expect": {
                        "finance@acme.test": {"documents": [key]},
                        "engineer@acme.test": {"answerable": False, "must_not_see": [key]},
                    },
                    "tags": ["generated", "exact-id", "en", "invoice"],
                }
            )
    return docs, questions


def _hr_restricted() -> list[GeneratedDoc]:
    return [
        GeneratedDoc(
            "hr-confidential",
            "investigation-2026-07.md",
            """# Workplace Investigation 2026-07 (Restricted)

Restricted HR case file: CANARY-HRR-8PL2.

Case HR-2026-07 concerns a conflict-of-interest report filed on 9 July 2026 in the Madrid office. A procurement
employee approved purchases from a supplier owned by a relative. Outcome: the employee was moved to another team and
the supplier relationship is under review by Finance. Case closed on 28 July 2026.
""",
        ),
        GeneratedDoc(
            "hr-confidential",
            "settlement-2026-05.md",
            """# Settlement Agreement 2026-05 (Restricted)

Restricted HR case file: CANARY-HRR-3VN7.

A settlement was signed on 14 May 2026 with a former sales employee after a dispute about unpaid commissions.
ACME paid 18,500 EUR and both parties agreed to confidentiality. Only the HR director and legal counsel hold copies.
""",
        ),
    ]


def generate() -> Generated:
    rng = random.Random(SEED)
    docs: list[GeneratedDoc] = []
    questions: list[dict[str, Any]] = []
    for part in (_contracts, _support, _catalog, _invoices):
        d, q = part(rng)
        docs += d
        questions += q
    docs += _hr_restricted()
    return Generated(docs, questions)


GENERATED_SET = SETS_DIR / "acme.generated.yaml"
_HEADER = "# Generated by `python -m app.eval.corpus` from a fixed seed. Do not edit by hand.\n"


def render(g: Generated) -> dict[Path, str]:
    """Every file the generator owns, with its content."""
    out = {CORPUS_DIR / d.slug / d.filename: d.text for d in g.docs}
    out[GENERATED_SET] = _HEADER + yaml.safe_dump(
        {"items": g.questions}, allow_unicode=True, sort_keys=False, width=120
    )
    return out


def write(g: Generated | None = None) -> int:
    files = render(g or generate())
    for d in {p.parent for p in files}:
        d.mkdir(parents=True, exist_ok=True)
    for owned in CORPUS_DIR.glob("*/*.md"):
        if owned not in files:
            owned.unlink()
    for path, text in files.items():
        path.write_text(text, encoding="utf-8")
    return len(files)


if __name__ == "__main__":
    print(f"wrote {write()} files")

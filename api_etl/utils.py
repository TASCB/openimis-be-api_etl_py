import csv
import datetime
import importlib
import inspect
import io
import pkgutil
import re
import zipfile
from typing import Any, Optional, List, Dict, Type, Iterable

from django.core.files.uploadedfile import InMemoryUploadedFile

# -------------------- ETL class discovery --------------------

ETL_CLASS = "api_etl.services"
ETL_BASE_PATH = "api_etl.services.base"


def _import_base_class(module_name: str = ETL_BASE_PATH) -> Optional[Type]:
    try:
        mod = importlib.import_module(module_name)
        return getattr(mod, "ETLService", None)
    except Exception:
        return None


def get_class_by_name(module_name: str, class_name: str):
    try:
        pkg = importlib.import_module(module_name)
        if hasattr(pkg, class_name):
            return getattr(pkg, class_name)
    except ModuleNotFoundError:
        raise ImportError(f"Module '{module_name}' not found.")

    if hasattr(pkg, "__path__"):
        for _, subname, _ in pkgutil.iter_modules(pkg.__path__, prefix=f"{module_name}."):
            try:
                sub = importlib.import_module(subname)
                if hasattr(sub, class_name):
                    return getattr(sub, class_name)
            except Exception:
                continue

    raise ImportError(f"Class '{class_name}' not found in package '{module_name}' (or its submodules).")


def get_classes_in_module(module_name: str) -> List[str]:
    try:
        pkg = importlib.import_module(module_name)
    except ModuleNotFoundError:
        raise ImportError(f"Module '{module_name}' not found.")

    Base = _import_base_class()
    found: List[Type] = []

    def collect_from(mod):
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            if not obj.__module__.startswith(module_name):
                continue
            if obj.__module__ == ETL_BASE_PATH:
                continue
            if Base is not None:
                if issubclass(obj, Base) and obj is not Base:
                    found.append(obj)
            else:
                found.append(obj)

    collect_from(pkg)

    if hasattr(pkg, "__path__"):
        for _, subname, _ in pkgutil.iter_modules(pkg.__path__, prefix=f"{module_name}."):
            try:
                sub = importlib.import_module(subname)
                collect_from(sub)
            except Exception:
                continue

    seen = set()
    names: List[str] = []
    for cls in found:
        n = cls.__name__
        if n not in seen:
            seen.add(n)
            names.append(n)
    return names


# -------------------- Batch ID --------------------

def get_timestamped_batch_identifier(prefix: str = "batch_") -> str:
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    return f"{prefix}{timestamp}"


# -------------------- CSV file creation --------------------

def data_to_file(data: List[Dict], identifier: Optional[Any] = None) -> InMemoryUploadedFile:
    if not data:
        raise ValueError("The data is empty and cannot be written to a file.")

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(data[0].keys()))
    writer.writeheader()
    writer.writerows(data)

    csv_bytes = buffer.getvalue().encode("utf-8")
    filename = f"{identifier or 'data'}.csv"

    return InMemoryUploadedFile(
        file=io.BytesIO(csv_bytes),
        field_name="file",
        name=filename,
        content_type="text/csv",
        size=len(csv_bytes),
        charset="utf-8",
    )


# -------------------- Nested dict navigation --------------------

def dig_path(obj: Dict, dotted: str):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


# -------------------- Date/datetime utilities --------------------

def to_datetime_str(date_in: Any) -> Optional[str]:
    """
    Convert date/datetime/string to standardized datetime string: YYYY-MM-DDTHH:MM:SS.mmm
    """
    if date_in is None:
        return None
    elif isinstance(date_in, datetime.datetime):
        return (date_in.isoformat() + ".000")[:23]
    elif isinstance(date_in, datetime.date):
        return (date_in.isoformat() + "T00:00:00.000")[:23]
    elif isinstance(date_in, str):
        regex_dt = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3,6})?$")
        if regex_dt.match(date_in):
            return (date_in + ".000")[:23]
        regex_d = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        if regex_d.match(date_in):
            return (date_in + "T00:00:00.000")[:23]
        return None
    else:
        return None


def to_date_str(date_in: Any) -> Optional[str]:
    """
    Convert date/datetime/string to date only: YYYY-MM-DD
    """
    if date_in is None:
        return None
    elif isinstance(date_in, (datetime.datetime, datetime.date)):
        return date_in.isoformat()[:10]
    elif isinstance(date_in, str):
        regex = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d{3,6})?)?$")
        if regex.match(date_in):
            return date_in[:10]
        return None
    else:
        return None


# -------------------- HTTP auth helpers --------------------

def auth_kwargs() -> Dict[str, Any]:
    """
    Build requests kwargs for auth based on ApiEtlConfig.
    - basic: {'auth': (username, password)}
    - bearer: {'headers': {'Authorization': 'Bearer <token>'}}
    - noauth: {}
    """
    from api_etl.apps import ApiEtlConfig as C  # local import to avoid cycles
    at = (getattr(C, "auth_type", "basic") or "basic").lower()
    if at == "bearer" and getattr(C, "auth_bearer_token", ""):
        return {"headers": {"Authorization": f"Bearer {C.auth_bearer_token}"}}
    if at == "basic":
        return {"auth": (C.auth_basic_username, C.auth_basic_password)}
    return {}


# -------------------- Export ZIP iteration (BOM-safe) --------------------

def iter_tab_files(zip_path: str, tab_name_contains: Optional[str] = None) -> Iterable[Dict[str, Any]]:
    """
    Iterate rows (dict) from .tab files inside a Survey Solutions export ZIP.
    - utf-8-sig reader strips BOM on first header
    - also strips any lingering BOM from keys defensively
    """
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if not member.lower().endswith(".tab"):
                continue
            if tab_name_contains and tab_name_contains.lower() not in member.lower():
                continue
            with zf.open(member) as fh:
                text = io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
                reader = csv.DictReader(text, delimiter="\t")
                if reader.fieldnames:
                    reader.fieldnames = [(fn or "").lstrip("\ufeff") for fn in reader.fieldnames]
                for row in reader:
                    yield { (k or "").lstrip("\ufeff"): v for k, v in row.items() }

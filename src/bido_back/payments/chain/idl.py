"""The `bido_escrow` program's IDL, parsed into typed values.

`bido_escrow.json` next to this module is a copy of `solana/idl/bido_escrow.json`
(the Docker image excludes `solana/`); a test fails when the two differ. `load_idl()`
reads it into a typed model: every instruction (discriminator, accounts in order
with their flags, arguments), the account and event discriminators, the program
errors and the type definitions `borsh.py` walks. An IDL shape the coder cannot
handle fails at load, never at encode time.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, Protocol, cast, get_args

IDL_FILE: Final = Path(__file__).resolve().parent / "bido_escrow.json"

type Primitive = Literal[
    "bool",
    "u8",
    "i8",
    "u16",
    "i16",
    "u32",
    "i32",
    "u64",
    "i64",
    "u128",
    "i128",
    "string",
    "bytes",
    "pubkey",
]
_PRIMITIVES: Final[frozenset[str]] = frozenset(get_args(Primitive.__value__))
_DISCRIMINATOR_LEN: Final = 8


@dataclass(frozen=True, slots=True)
class ArrayType:
    """`[item; length]`."""

    item: "IdlType"
    length: int


@dataclass(frozen=True, slots=True)
class VecType:
    """`Vec<item>`: u32 LE count, then the items."""

    item: "IdlType"


@dataclass(frozen=True, slots=True)
class OptionType:
    """`Option<item>`: u8 0 or 1, then the item if 1."""

    item: "IdlType"


@dataclass(frozen=True, slots=True)
class DefinedType:
    """A struct or enum of the IDL's `types`, by name."""

    name: str


type IdlType = Primitive | ArrayType | VecType | OptionType | DefinedType


@dataclass(frozen=True, slots=True)
class IdlField:
    name: str
    type: IdlType


@dataclass(frozen=True, slots=True)
class StructDef:
    """A struct with named fields, encoded in field order."""

    name: str
    fields: tuple[IdlField, ...]


@dataclass(frozen=True, slots=True)
class EnumDef:
    """An enum of unit variants, encoded as the u8 index of the variant."""

    name: str
    variants: tuple[str, ...]


type TypeDef = StructDef | EnumDef


@dataclass(frozen=True, slots=True)
class IdlInstructionAccount:
    name: str
    writable: bool
    signer: bool
    # Set when the IDL pins the account (the system and associated token programs).
    address: str | None


@dataclass(frozen=True, slots=True)
class IdlInstruction:
    name: str
    discriminator: bytes
    accounts: tuple[IdlInstructionAccount, ...]
    args: tuple[IdlField, ...]


@dataclass(frozen=True, slots=True)
class IdlErrorCode:
    code: int
    name: str
    msg: str


@dataclass(frozen=True, slots=True)
class Idl:
    """The program IDL; account and event maps hold their 8-byte discriminators by name."""

    address: str
    instructions: Mapping[str, IdlInstruction]
    accounts: Mapping[str, bytes]
    events: Mapping[str, bytes]
    errors: tuple[IdlErrorCode, ...]
    types: Mapping[str, TypeDef]


@cache
def load_idl() -> Idl:
    """The IDL copy next to this module, parsed once."""
    return parse_idl(json.loads(IDL_FILE.read_text(encoding="utf-8")))


@cache
def program_error_names() -> Mapping[int, str]:
    """Anchor error code (6000 + the `EscrowError` index) -> the error's name."""
    return MappingProxyType({error.code: error.name for error in load_idl().errors})


def parse_idl(raw: object) -> Idl:
    """Parses an Anchor 1.x IDL JSON value; raises `ValueError` on any unsupported shape."""
    idl = _obj(raw, "IDL")
    types = _by_name([_type_def(t) for t in _list(idl.get("types"), "types")], "type")
    for type_def in types.values():
        if isinstance(type_def, StructDef):
            for field in type_def.fields:
                _check_defined(field.type, types)
    raw_instructions = _list(idl.get("instructions"), "instructions")
    instructions = _by_name([_instruction(i) for i in raw_instructions], "instruction")
    for instruction in instructions.values():
        for arg in instruction.args:
            _check_defined(arg.type, types)
    accounts = _discriminators(idl.get("accounts"), "accounts")
    events = _discriminators(idl.get("events"), "events")
    for name in [*accounts, *events]:
        _check_defined(DefinedType(name), types)
    errors = tuple(
        IdlErrorCode(
            code=_int(error.get("code"), "error code"),
            name=_str(error.get("name"), "error name"),
            msg=_str(error.get("msg"), "error msg"),
        )
        for error in (_obj(e, "error") for e in _list(idl.get("errors"), "errors"))
    )
    return Idl(
        address=_str(idl.get("address"), "address"),
        instructions=instructions,
        accounts=accounts,
        events=events,
        errors=errors,
        types=types,
    )


def _instruction(raw: object) -> IdlInstruction:
    ix = _obj(raw, "instruction")
    name = _str(ix.get("name"), "instruction name")
    accounts: list[IdlInstructionAccount] = []
    for raw_account in _list(ix.get("accounts"), f"{name} accounts"):
        account = _obj(raw_account, f"{name} account")
        if "accounts" in account:
            raise ValueError(f"{name}: nested account groups are not supported")
        if account.get("optional", False):
            raise ValueError(f"{name}: optional accounts are not supported")
        address = account.get("address")
        accounts.append(
            IdlInstructionAccount(
                name=_str(account.get("name"), f"{name} account name"),
                writable=_bool(account.get("writable", False), "writable"),
                signer=_bool(account.get("signer", False), "signer"),
                address=None if address is None else _str(address, "address"),
            )
        )
    return IdlInstruction(
        name=name,
        discriminator=_discriminator(ix.get("discriminator"), name),
        accounts=tuple(accounts),
        args=tuple(_field(arg) for arg in _list(ix.get("args"), f"{name} args")),
    )


def _type_def(raw: object) -> TypeDef:
    type_def = _obj(raw, "type definition")
    name = _str(type_def.get("name"), "type name")
    if "generics" in type_def or "serialization" in type_def or "repr" in type_def:
        raise ValueError(f"type {name}: generics, serialization and repr are not supported")
    body = _obj(type_def.get("type"), f"type {name}")
    kind = body.get("kind")
    if kind == "struct":
        fields = _list(body.get("fields", []), f"{name} fields")
        return StructDef(name=name, fields=tuple(_field(f) for f in fields))
    if kind == "enum":
        variants: list[str] = []
        for raw_variant in _list(body.get("variants"), f"{name} variants"):
            variant = _obj(raw_variant, f"{name} variant")
            if "fields" in variant:
                raise ValueError(f"enum {name}: variants with fields are not supported")
            variants.append(_str(variant.get("name"), f"{name} variant name"))
        if not 0 < len(variants) <= 256:
            raise ValueError(f"enum {name}: needs 1 to 256 variants")
        return EnumDef(name=name, variants=tuple(variants))
    raise ValueError(f"type {name}: kind {kind!r} is not supported")


def _field(raw: object) -> IdlField:
    # A tuple struct lists bare types instead of `{ name, type }` objects.
    field = _obj(raw, "field") if not isinstance(raw, str) else {}
    name = field.get("name")
    if not isinstance(name, str):
        raise ValueError(f"tuple fields are not supported: {raw!r}")
    return IdlField(name=name, type=_type(field.get("type")))


def _type(raw: object) -> IdlType:
    if isinstance(raw, str):
        if raw not in _PRIMITIVES:
            raise ValueError(f"IDL type {raw!r} is not supported")
        return cast(Primitive, raw)
    ty = _obj(raw, "IDL type")
    if len(ty) != 1:
        raise ValueError(f"IDL type {ty!r} is not supported")
    if "array" in ty:
        array = _list(ty.get("array"), "array")
        if len(array) != 2:
            raise ValueError(f"array type {array!r} must be [item, length]")
        item, length = array
        if not isinstance(length, int) or isinstance(length, bool) or length < 0:
            raise ValueError(f"array length must be a non-negative integer, got {length!r}")
        return ArrayType(item=_type(item), length=length)
    if "vec" in ty:
        return VecType(item=_type(ty.get("vec")))
    if "option" in ty:
        return OptionType(item=_type(ty.get("option")))
    if "defined" in ty:
        defined = _obj(ty.get("defined"), "defined")
        if "generics" in defined:
            raise ValueError(f"generic type {defined!r} is not supported")
        return DefinedType(name=_str(defined.get("name"), "defined name"))
    raise ValueError(f"IDL type {ty!r} is not supported")


def _check_defined(ty: IdlType, types: Mapping[str, TypeDef]) -> None:
    match ty:
        case ArrayType(item=item) | VecType(item=item) | OptionType(item=item):
            _check_defined(item, types)
        case DefinedType(name=name) if name not in types:
            raise ValueError(f"type {name!r} is not defined in the IDL")
        case _:
            pass


def _discriminators(raw: object, what: str) -> Mapping[str, bytes]:
    entries = [_obj(entry, what) for entry in _list(raw, what)]
    names = [_str(entry.get("name"), f"{what} name") for entry in entries]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate name in IDL {what}")
    return MappingProxyType(
        {
            name: _discriminator(entry.get("discriminator"), name)
            for name, entry in zip(names, entries, strict=True)
        }
    )


def _discriminator(raw: object, name: str) -> bytes:
    values = _list(raw, f"{name} discriminator")
    if len(values) != _DISCRIMINATOR_LEN or not all(
        isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255 for v in values
    ):
        raise ValueError(f"{name}: the discriminator must be 8 bytes")
    return bytes(cast(list[int], values))


class _Named(Protocol):
    @property
    def name(self) -> str: ...


def _by_name[T: _Named](items: list[T], what: str) -> Mapping[str, T]:
    by_name: dict[str, T] = {}
    for item in items:
        if item.name in by_name:
            raise ValueError(f"duplicate {what} {item.name!r} in the IDL")
        by_name[item.name] = item
    return MappingProxyType(by_name)


def _obj(raw: object, what: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError(f"{what} must be a JSON object")
    return cast(dict[str, object], raw)


def _list(raw: object, what: str) -> list[object]:
    if not isinstance(raw, list):
        raise ValueError(f"{what} must be a JSON array")
    return cast(list[object], raw)


def _str(raw: object, what: str) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"{what} must be a string")
    return raw


def _int(raw: object, what: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ValueError(f"{what} must be an integer")
    return raw


def _bool(raw: object, what: str) -> bool:
    if not isinstance(raw, bool):
        raise ValueError(f"{what} must be a boolean")
    return raw

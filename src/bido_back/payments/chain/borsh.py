"""An IDL-driven Borsh encoder and decoder for the `bido_escrow` program.

Values are plain Python: integers and `bool` as themselves, `string` as `str`,
`bytes` and every array or vec of `u8` as `bytes`, `pubkey` as a solders `Pubkey`,
other arrays and vecs as `list`, `Option` as the value or `None`, structs as a
`dict` of exactly their fields, and enums as the variant name. The `as_*` helpers
narrow a decoded value for typed callers.
"""

from collections.abc import Mapping
from typing import Final

from solders.pubkey import Pubkey

from bido_back.payments.chain.idl import (
    ArrayType,
    DefinedType,
    EnumDef,
    Idl,
    IdlField,
    IdlType,
    OptionType,
    StructDef,
    VecType,
)

type BorshValue = (
    bool | int | str | bytes | Pubkey | None | list[BorshValue] | dict[str, BorshValue]
)

# Integer type -> (size in bytes, signed).
_INTS: Final[Mapping[str, tuple[int, bool]]] = {
    "u8": (1, False),
    "i8": (1, True),
    "u16": (2, False),
    "i16": (2, True),
    "u32": (4, False),
    "i32": (4, True),
    "u64": (8, False),
    "i64": (8, True),
    "u128": (16, False),
    "i128": (16, True),
}
_LEN_BYTES: Final = 4
_PUBKEY_LEN: Final = 32


class BorshCoder:
    """Encodes and decodes the IDL's types, accounts and instruction data."""

    def __init__(self, idl: Idl) -> None:
        self._idl = idl

    def encode(self, ty: IdlType, value: BorshValue) -> bytes:
        out = bytearray()
        self._encode(ty, value, out, "value")
        return bytes(out)

    def decode(self, ty: IdlType, data: bytes) -> BorshValue:
        """Decodes `data`, which must hold exactly one `ty` value."""
        reader = _Reader(data)
        value = self._decode(ty, reader)
        reader.expect_end()
        return value

    def encode_account(self, name: str, value: BorshValue) -> bytes:
        """`name`'s discriminator, then `value` (no padding to the account size)."""
        return self._idl.accounts[name] + self.encode(DefinedType(name), value)

    def decode_account(self, name: str, data: bytes) -> dict[str, BorshValue]:
        """Decodes account data of type `name` after checking its discriminator.

        Bytes after the value are ignored, as Anchor does: an account is allocated at
        its maximum size and its tail stays zero.
        """
        discriminator = self._idl.accounts[name]
        if data[: len(discriminator)] != discriminator:
            raise ValueError(f"account data is not a {name} (discriminator mismatch)")
        reader = _Reader(data, len(discriminator))
        return as_struct(self._decode(DefinedType(name), reader))

    def encode_instruction(self, name: str, args: Mapping[str, BorshValue]) -> bytes:
        """`name`'s discriminator, then each IDL argument in order."""
        instruction = self._idl.instructions[name]
        out = bytearray(instruction.discriminator)
        self._encode_fields(instruction.args, args, out, name)
        return bytes(out)

    def decode_instruction(self, data: bytes) -> tuple[str, dict[str, BorshValue]]:
        """The instruction name and its arguments; trailing bytes are an error."""
        for instruction in self._idl.instructions.values():
            discriminator = instruction.discriminator
            if data[: len(discriminator)] == discriminator:
                reader = _Reader(data, len(discriminator))
                args = self._decode_fields(instruction.args, reader)
                reader.expect_end()
                return instruction.name, args
        raise ValueError("instruction data matches no instruction of the IDL")

    def _encode(self, ty: IdlType, value: BorshValue, out: bytearray, path: str) -> None:
        match ty:
            case "bool":
                if not isinstance(value, bool):
                    raise ValueError(f"{path}: expected a bool")
                out.append(1 if value else 0)
            case "string":
                if not isinstance(value, str):
                    raise ValueError(f"{path}: expected a str")
                _put_bytes(value.encode(), out, path)
            case "bytes":
                _put_bytes(_bytes(value, path), out, path)
            case "pubkey":
                if not isinstance(value, Pubkey):
                    raise ValueError(f"{path}: expected a Pubkey")
                out += bytes(value)
            case ArrayType(item="u8", length=length):
                raw = _bytes(value, path)
                if len(raw) != length:
                    raise ValueError(f"{path}: expected {length} bytes, got {len(raw)}")
                out += raw
            case ArrayType(item=item, length=length):
                items = _list(value, path)
                if len(items) != length:
                    raise ValueError(f"{path}: expected {length} items, got {len(items)}")
                for i, element in enumerate(items):
                    self._encode(item, element, out, f"{path}[{i}]")
            case VecType(item="u8"):
                _put_bytes(_bytes(value, path), out, path)
            case VecType(item=item):
                items = _list(value, path)
                _put_len(len(items), out, path)
                for i, element in enumerate(items):
                    self._encode(item, element, out, f"{path}[{i}]")
            case OptionType(item=item):
                if value is None:
                    out.append(0)
                else:
                    out.append(1)
                    self._encode(item, value, out, path)
            case DefinedType(name=name):
                self._encode_defined(name, value, out, path)
            case _:
                size, signed = _INTS[ty]
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"{path}: expected an integer")
                try:
                    out += value.to_bytes(size, "little", signed=signed)
                except OverflowError:
                    raise ValueError(f"{path}: the value does not fit in {ty}") from None

    def _encode_defined(self, name: str, value: BorshValue, out: bytearray, path: str) -> None:
        match self._idl.types[name]:
            case StructDef(fields=fields):
                if not isinstance(value, dict):
                    raise ValueError(f"{path}: expected a {name} dict")
                self._encode_fields(fields, value, out, path)
            case EnumDef(variants=variants):
                if not isinstance(value, str) or value not in variants:
                    raise ValueError(f"{path}: expected a {name} variant {variants}")
                out.append(variants.index(value))

    def _encode_fields(
        self,
        fields: tuple[IdlField, ...],
        values: Mapping[str, BorshValue],
        out: bytearray,
        path: str,
    ) -> None:
        names = [field.name for field in fields]
        if sorted(values) != sorted(names):
            raise ValueError(f"{path}: expected the fields {names}, got {sorted(values)}")
        for field in fields:
            self._encode(field.type, values[field.name], out, f"{path}.{field.name}")

    def _decode(self, ty: IdlType, reader: "_Reader") -> BorshValue:
        match ty:
            case "bool":
                flag = reader.take(1)[0]
                if flag > 1:
                    raise ValueError(f"invalid bool byte {flag} at offset {reader.offset - 1}")
                return flag == 1
            case "string":
                raw = reader.take(reader.length())
                try:
                    return raw.decode()
                except UnicodeDecodeError:
                    raise ValueError(f"string before offset {reader.offset} is not UTF-8") from None
            case "bytes" | VecType(item="u8"):
                return reader.take(reader.length())
            case "pubkey":
                return Pubkey.from_bytes(reader.take(_PUBKEY_LEN))
            case ArrayType(item="u8", length=length):
                return reader.take(length)
            case ArrayType(item=item, length=length):
                return [self._decode(item, reader) for _ in range(length)]
            case VecType(item=item):
                return [self._decode(item, reader) for _ in range(reader.length())]
            case OptionType(item=item):
                tag = reader.take(1)[0]
                if tag > 1:
                    raise ValueError(f"invalid option tag {tag} at offset {reader.offset - 1}")
                return self._decode(item, reader) if tag == 1 else None
            case DefinedType(name=name):
                match self._idl.types[name]:
                    case StructDef(fields=fields):
                        return self._decode_fields(fields, reader)
                    case EnumDef(variants=variants):
                        index = reader.take(1)[0]
                        if index >= len(variants):
                            raise ValueError(f"{name} has no variant {index}")
                        return variants[index]
            case _:
                size, signed = _INTS[ty]
                return int.from_bytes(reader.take(size), "little", signed=signed)

    def _decode_fields(
        self, fields: tuple[IdlField, ...], reader: "_Reader"
    ) -> dict[str, BorshValue]:
        return {field.name: self._decode(field.type, reader) for field in fields}


def as_int(value: BorshValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("expected an integer")
    return value


def as_str(value: BorshValue) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a str")
    return value


def as_bytes(value: BorshValue) -> bytes:
    if not isinstance(value, bytes):
        raise ValueError("expected bytes")
    return value


def as_pubkey(value: BorshValue) -> Pubkey:
    if not isinstance(value, Pubkey):
        raise ValueError("expected a Pubkey")
    return value


def as_list(value: BorshValue) -> list[BorshValue]:
    if not isinstance(value, list):
        raise ValueError("expected a list")
    return value


def as_struct(value: BorshValue) -> dict[str, BorshValue]:
    if not isinstance(value, dict):
        raise ValueError("expected a struct")
    return value


class _Reader:
    def __init__(self, data: bytes, offset: int = 0) -> None:
        self._data = data
        self.offset = offset

    def take(self, size: int) -> bytes:
        end = self.offset + size
        if end > len(self._data):
            raise ValueError(
                f"data ends at {len(self._data)} bytes, {size} more needed at offset {self.offset}"
            )
        chunk = self._data[self.offset : end]
        self.offset = end
        return chunk

    def length(self) -> int:
        """A u32 LE length or count prefix."""
        return int.from_bytes(self.take(_LEN_BYTES), "little")

    def expect_end(self) -> None:
        if self.offset != len(self._data):
            raise ValueError(f"{len(self._data) - self.offset} trailing bytes after the value")


def _bytes(value: BorshValue, path: str) -> bytes:
    if not isinstance(value, bytes):
        raise ValueError(f"{path}: expected bytes")
    return value


def _list(value: BorshValue, path: str) -> list[BorshValue]:
    if not isinstance(value, list):
        raise ValueError(f"{path}: expected a list")
    return value


def _put_len(length: int, out: bytearray, path: str) -> None:
    if length >= 1 << 32:
        raise ValueError(f"{path}: {length} items do not fit a u32 length")
    out += length.to_bytes(_LEN_BYTES, "little")


def _put_bytes(raw: bytes, out: bytearray, path: str) -> None:
    _put_len(len(raw), out, path)
    out += raw

"""Minimal reader for Android binary XML (AXML), enough to read manifest meta-data.

The official index never relies on a plugin repository's hand-written ABI declaration
when the released APK itself says what it supports: the shared `nativeAlignment` Gradle
plugin (1.8.4+) writes `org.autojs.plugin.contract.SUPPORTED_ABIS` into the merged
manifest from the native libraries it actually packaged. This module extracts that
`<meta-data>` value from the compiled `AndroidManifest.xml` inside an APK without any
external tooling.
"""
import struct

RES_XML_TYPE = 0x0003
RES_STRING_POOL_TYPE = 0x0001
RES_XML_START_ELEMENT_TYPE = 0x0102
RES_XML_END_ELEMENT_TYPE = 0x0103

UTF8_FLAG = 0x100
TYPE_REFERENCE = 0x01
TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11
TYPE_INT_BOOLEAN = 0x12

ANDROID_NAMESPACE = "http://schemas.android.com/apk/res/android"


class AxmlError(ValueError):
    pass


def _string_pool(data, offset, size):
    string_count, style_count, flags, strings_start, _styles_start = struct.unpack_from("<IIIII", data, offset + 8)
    utf8 = bool(flags & UTF8_FLAG)
    offsets = struct.unpack_from("<%dI" % string_count, data, offset + 28)
    base = offset + strings_start
    strings = []
    for relative in offsets:
        position = base + relative
        if position >= offset + size:
            raise AxmlError("String pool offset escapes the chunk")
        if utf8:
            # UTF-8 strings carry the UTF-16 length then the byte length, each 1 or 2 bytes.
            _, position = _utf8_length(data, position)
            byte_length, position = _utf8_length(data, position)
            strings.append(data[position:position + byte_length].decode("utf-8", "replace"))
        else:
            length = struct.unpack_from("<H", data, position)[0]
            position += 2
            if length & 0x8000:
                length = ((length & 0x7FFF) << 16) | struct.unpack_from("<H", data, position)[0]
                position += 2
            strings.append(data[position:position + length * 2].decode("utf-16-le", "replace"))
    return strings


def _utf8_length(data, position):
    value = data[position]
    position += 1
    if value & 0x80:
        value = ((value & 0x7F) << 8) | data[position]
        position += 1
    return value, position


def _attribute_value(strings, raw_index, data_type, data_value):
    if raw_index != 0xFFFFFFFF and raw_index < len(strings):
        return strings[raw_index]
    if data_type == TYPE_STRING:
        return strings[data_value] if data_value < len(strings) else None
    if data_type in (TYPE_INT_DEC, TYPE_INT_HEX):
        return str(struct.unpack("<i", struct.pack("<I", data_value))[0])
    if data_type == TYPE_INT_BOOLEAN:
        return "true" if data_value else "false"
    if data_type == TYPE_REFERENCE:
        return "@0x%08x" % data_value
    return None


def parse_elements(data):
    """Yield (depth, element_name, {attribute_name: value}) for every start element.

    Attribute names are returned as `namespace|name` when namespaced and `name` otherwise.
    """
    if len(data) < 8:
        raise AxmlError("Binary XML is too short")
    chunk_type, header_size, chunk_size = struct.unpack_from("<HHI", data, 0)
    if chunk_type != RES_XML_TYPE or chunk_size > len(data):
        raise AxmlError("Not an Android binary XML document")
    strings = []
    depth = 0
    offset = header_size
    while offset + 8 <= chunk_size:
        chunk_type, header_size, size = struct.unpack_from("<HHI", data, offset)
        if size < 8 or offset + size > chunk_size:
            raise AxmlError("Malformed binary XML chunk")
        if chunk_type == RES_STRING_POOL_TYPE:
            strings = _string_pool(data, offset, size)
        elif chunk_type == RES_XML_START_ELEMENT_TYPE:
            namespace_index, name_index = struct.unpack_from("<II", data, offset + 16)
            attribute_start, _attribute_size, attribute_count = struct.unpack_from("<HHH", data, offset + 24)
            position = offset + 16 + attribute_start
            attributes = {}
            for _ in range(attribute_count):
                namespace, name, raw_value, _size, _res0, data_type, value = struct.unpack_from("<IIIHBBI", data, position)
                position += 20
                attribute_name = strings[name] if name < len(strings) else ""
                if namespace != 0xFFFFFFFF and namespace < len(strings):
                    attribute_name = strings[namespace] + "|" + attribute_name
                attributes[attribute_name] = _attribute_value(strings, raw_value, data_type, value)
            yield depth, strings[name_index] if name_index < len(strings) else "", attributes
            depth += 1
        elif chunk_type == RES_XML_END_ELEMENT_TYPE:
            depth -= 1
        offset += size


def manifest_meta_data(data):
    """Return {name: value} for every `<meta-data>` directly under `<application>`."""
    values = {}
    application_depth = None
    for depth, name, attributes in parse_elements(data):
        if name == "application":
            application_depth = depth
            continue
        if application_depth is None or depth != application_depth + 1:
            continue
        if name != "meta-data":
            continue
        meta_name = attributes.get(ANDROID_NAMESPACE + "|name")
        if meta_name:
            values[meta_name] = attributes.get(ANDROID_NAMESPACE + "|value")
    return values

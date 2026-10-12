"""Same H.264 NAL classification as smartcut 1.7, using C byte searching."""
import threading

_index_lock = threading.Lock()


def h264_nal_type(packet_data):
    if not packet_data or len(packet_data) < 5:
        return None
    data = bytes(packet_data)
    size = len(data)
    nal_length = int.from_bytes(data[:4], 'big')
    # Preserve the pinned parser's length-prefix priority exactly, including
    # short AUD prefixes that fall through to the Annex B scan.
    if 4 < nal_length <= size - 4:
        return data[4] & 31
    types = []
    offset = 0
    while True:
        found = data.find(b'\x00\x00\x01', offset)
        if found < 0:
            break
        start = found - 1 if found > offset and data[found - 1] == 0 else found
        if start >= size - 4:
            break
        header = found + 3
        kind = data[header] & 31
        if kind == 5:
            return 5
        types.append(kind)
        offset = header
    return next((kind for kind in types if 1 <= kind <= 4), types[0] if types else None)


def index_media(path, container_type):
    import smartcut.media_container as module
    # The dependency files remain untouched. Serialize this narrow temporary
    # substitution and restore the module even if input indexing fails.
    with _index_lock:
        original = module.get_h264_nal_unit_type
        module.get_h264_nal_unit_type = h264_nal_type
        try:
            return container_type(str(path))
        finally:
            module.get_h264_nal_unit_type = original

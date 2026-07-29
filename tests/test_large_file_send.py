"""Large-file download framing coverage."""

import asyncio

from pygserver.filesystem import FileSystem
from pygserver.protocol.constants import PLO
from pygserver.protocol.packet_codec import PacketReader


class RecordingPlayer:
    def __init__(self):
        self.id = 17
        self.packets = []

    async def send_raw(self, packet):
        self.packets.append(packet)


def test_large_file_chunks_have_file_headers_and_named_end(tmp_path, monkeypatch):
    def run_immediately(loop, executor, function, *args):
        future = loop.create_future()
        try:
            future.set_result(function(*args))
        except Exception as error:
            future.set_exception(error)
        return future

    monkeypatch.setattr(asyncio.BaseEventLoop, "run_in_executor", run_immediately)
    payload = b"first\nchunk\x00second chunk"
    path = tmp_path / "images" / "map.bin"
    path.parent.mkdir()
    path.write_bytes(payload)
    filesystem = FileSystem(object(), str(tmp_path))
    filesystem.chunk_size = 8
    player = RecordingPlayer()

    asyncio.run(filesystem._send_large_file(player, "map.bin", path))

    chunk_frames = player.packets[2:-1]
    rebuilt = bytearray()
    for frame in chunk_frames:
        announcement = PacketReader(frame)
        assert announcement.read_gchar() == PLO.RAWDATA
        raw_size = announcement.read_gint3()
        assert announcement.read_byte() == ord("\n")
        file_packet = frame[announcement.pos:]
        assert raw_size == len(file_packet)

        packet = PacketReader(file_packet)
        assert packet.read_gchar() == PLO.FILE
        assert packet.read_gint5() == 0
        name_length = packet.read_gchar()
        assert packet.read_bytes(name_length) == b"map.bin"
        rebuilt.extend(packet.read_bytes(len(packet.remaining()) - 1))
        assert packet.read_byte() == ord("\n")

    assert bytes(rebuilt) == payload
    assert player.packets[-1] == bytes([PLO.LARGEFILEEND + 32]) + b"map.bin\n"

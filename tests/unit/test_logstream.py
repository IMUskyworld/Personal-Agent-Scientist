"""Streaming log writer: UTF-8 output, GBK fallback, nothing buffered in memory."""

from __future__ import annotations

from pathlib import Path

from labloop.execute.logstream import SNIFF_BYTES, LogStream


def test_logstream_writes_utf8_output(tmp_path: Path) -> None:
    path = tmp_path / "stdout.log"
    stream = LogStream(path)
    stream.write(b"hello ")
    stream.write("训练完成".encode())
    stream.close()
    assert path.read_text(encoding="utf-8") == "hello 训练完成"


def test_logstream_falls_back_to_gbk(tmp_path: Path) -> None:
    """Windows 训练脚本常用 GBK 输出：不能假设子进程用 UTF-8。"""
    path = tmp_path / "stdout.log"
    stream = LogStream(path)
    stream.write("结果: ".encode("gbk"))
    stream.write("0.9".encode("gbk"))
    stream.close()
    assert path.read_text(encoding="utf-8") == "结果: 0.9"


def test_logstream_chooses_encoding_after_the_sniff_window(tmp_path: Path) -> None:
    path = tmp_path / "stdout.log"
    stream = LogStream(path)
    stream.write(b"a" * (SNIFF_BYTES // 2))
    stream.write("中文".encode("gbk"))
    stream.write("中文".encode())
    stream.close()
    text = path.read_text(encoding="utf-8")
    assert "a" * 10 in text
    assert "中文" in text


def test_logstream_handles_empty_output(tmp_path: Path) -> None:
    path = tmp_path / "stdout.log"
    stream = LogStream(path)
    stream.close()
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == ""


def test_logstream_appends_text(tmp_path: Path) -> None:
    path = tmp_path / "stdout.log"
    stream = LogStream(path)
    stream.write(b"done\n")
    stream.close()
    stream.append_text("[用户中断]\n")
    assert path.read_text(encoding="utf-8") == "done\n[用户中断]\n"

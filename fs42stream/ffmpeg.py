from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from .planner import PlannedBlock


StreamProfile = Literal["direct", "jellyfin"]
PlayoutMode = Literal["hls-primary", "ts-primary"]

DIRECT_HLS_LIST_SIZE = 12
JELLYFIN_HLS_LIST_SIZE = 60


def hls_list_size_for_profile(stream_profile: StreamProfile) -> int:
    return JELLYFIN_HLS_LIST_SIZE if stream_profile == "jellyfin" else DIRECT_HLS_LIST_SIZE


class FFMpegHLSCommandBuilder:
    """Build normalized FFmpeg commands for playout and HLS packaging."""

    def __init__(self, ffmpeg: str = "/usr/bin/ffmpeg", *, video_encoder: str = "libx264", vaapi_device: str | None = None) -> None:
        self.ffmpeg = ffmpeg
        self.video_encoder = video_encoder
        self.vaapi_device = vaapi_device
        if self.video_encoder.endswith("_vaapi") and not self.vaapi_device:
            raise ValueError("vaapi_device is required when using a VAAPI video encoder")

    def build(
        self,
        block: PlannedBlock,
        *,
        output_dir: Path,
        duration_limit: float | None = None,
        output_name: str | None = None,
        hls_start_number: int = 0,
        hls_start_time_offset: float | None = None,
        hls_append: bool = False,
        stream_profile: StreamProfile = "direct",
    ) -> list[str]:
        if not block.items:
            raise ValueError("cannot build ffmpeg command for an empty block")
        if duration_limit is not None and duration_limit <= 0:
            raise ValueError("duration_limit must be positive")
        if hls_start_number < 0:
            raise ValueError("hls_start_number must be non-negative")
        if hls_start_time_offset is not None and hls_start_time_offset < 0:
            raise ValueError("hls_start_time_offset must be non-negative")
        if stream_profile not in {"direct", "jellyfin"}:
            raise ValueError("stream_profile must be 'direct' or 'jellyfin'")

        cmd = self._build_block_inputs(block, stream_profile=stream_profile)
        filter_complex = self._filter_complex(block, upload_to_vaapi=self.video_encoder.endswith("_vaapi"))
        output_slug = self._slug(output_name or block.title)
        playlist = output_dir / f"{output_slug}.m3u8"
        segment_pattern = output_dir / f"{output_slug}_%05d.ts"
        cmd.extend(self._normalized_video_audio_output_args(filter_complex))
        if duration_limit is not None:
            cmd.extend(["-t", self._num(duration_limit)])
        if stream_profile == "jellyfin" and hls_start_time_offset is not None:
            cmd.extend(["-output_ts_offset", self._num(hls_start_time_offset)])
        hls_args = ["-f", "hls", "-hls_time", "2", "-hls_list_size", str(hls_list_size_for_profile(stream_profile))]
        if not hls_append:
            hls_args.extend(["-start_number", str(hls_start_number)])
        hls_flags = ["omit_endlist"]
        if hls_append:
            hls_flags.append("append_list")
            if stream_profile != "jellyfin":
                hls_flags.append("discont_start")
        hls_args.extend(["-hls_flags", "+".join(hls_flags)])
        hls_args.extend(
            [
                "-hls_segment_filename",
                str(segment_pattern),
                str(playlist),
            ]
        )
        cmd.extend(hls_args)
        return cmd

    def build_transport_stream(
        self,
        block: PlannedBlock,
        *,
        output_path: Path,
        duration_limit: float | None = None,
    ) -> list[str]:
        if not block.items:
            raise ValueError("cannot build ffmpeg command for an empty block")
        if duration_limit is not None and duration_limit <= 0:
            raise ValueError("duration_limit must be positive")

        cmd = self._build_block_inputs(block, stream_profile="direct")
        filter_complex = self._filter_complex(block, upload_to_vaapi=self.video_encoder.endswith("_vaapi"))
        cmd.extend(self._normalized_video_audio_output_args(filter_complex))
        if duration_limit is not None:
            cmd.extend(["-t", self._num(duration_limit)])
        cmd.extend(["-f", "mpegts", "-muxdelay", "0", "-muxpreload", "0", str(output_path)])
        return cmd

    def build_hls_from_transport_stream(
        self,
        input_path: Path,
        *,
        output_dir: Path,
        output_name: str,
        hls_start_number: int = 0,
        hls_start_time_offset: float | None = None,
        hls_append: bool = False,
        stream_profile: StreamProfile = "direct",
        realtime_input: bool = True,
    ) -> list[str]:
        if hls_start_number < 0:
            raise ValueError("hls_start_number must be non-negative")
        if hls_start_time_offset is not None and hls_start_time_offset < 0:
            raise ValueError("hls_start_time_offset must be non-negative")
        if stream_profile not in {"direct", "jellyfin"}:
            raise ValueError("stream_profile must be 'direct' or 'jellyfin'")

        output_slug = self._slug(output_name)
        playlist = output_dir / f"{output_slug}.m3u8"
        segment_pattern = output_dir / f"{output_slug}_%05d.ts"
        cmd: list[str] = [self.ffmpeg, "-hide_banner", "-y"]
        fflags: list[str] = []
        if stream_profile == "jellyfin":
            fflags.append("genpts")
        if not realtime_input and str(input_path) == "pipe:0":
            cmd.extend(["-probesize", "32768", "-analyzeduration", "0"])
            fflags.append("nobuffer")
        if fflags:
            cmd.extend(["-fflags", "+" + "+".join(fflags)])
        if realtime_input:
            cmd.append("-re")
        cmd.extend(["-i", str(input_path), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy", "-c:a", "copy"])
        if stream_profile == "jellyfin" and hls_start_time_offset is not None:
            cmd.extend(["-output_ts_offset", self._num(hls_start_time_offset)])
        hls_args = ["-f", "hls", "-hls_time", "2", "-hls_list_size", str(hls_list_size_for_profile(stream_profile))]
        if not hls_append:
            hls_args.extend(["-start_number", str(hls_start_number)])
        hls_flags = ["omit_endlist"]
        if hls_append:
            hls_flags.append("append_list")
            if stream_profile != "jellyfin":
                hls_flags.append("discont_start")
        hls_args.extend(["-hls_flags", "+".join(hls_flags)])
        hls_args.extend(["-hls_segment_filename", str(segment_pattern), str(playlist)])
        cmd.extend(hls_args)
        return cmd

    def build_transport_stream_and_hls(
        self,
        block: PlannedBlock,
        *,
        output_dir: Path,
        output_name: str,
        transport_stream_path: Path,
        duration_limit: float | None = None,
        hls_start_number: int = 0,
        hls_start_time_offset: float | None = None,
        hls_append: bool = False,
        stream_profile: StreamProfile = "direct",
    ) -> list[str]:
        if hls_start_number < 0:
            raise ValueError("hls_start_number must be non-negative")
        if hls_start_time_offset is not None and hls_start_time_offset < 0:
            raise ValueError("hls_start_time_offset must be non-negative")
        if stream_profile not in {"direct", "jellyfin"}:
            raise ValueError("stream_profile must be 'direct' or 'jellyfin'")

        output_slug = self._slug(output_name)
        playlist = output_dir / f"{output_slug}.m3u8"
        segment_pattern = output_dir / f"{output_slug}_%05d.ts"
        hls_flags = ["omit_endlist"]
        if hls_append:
            hls_flags.append("append_list")
            if stream_profile != "jellyfin":
                hls_flags.append("discont_start")
        hls_options = [
            "f=hls",
            "hls_time=2",
            f"hls_list_size={hls_list_size_for_profile(stream_profile)}",
            f"hls_flags={'+'.join(hls_flags)}",
            f"hls_segment_filename={segment_pattern}",
        ]
        if not hls_append:
            hls_options.append(f"start_number={hls_start_number}")

        tee_output = "|".join(
            [
                f"[f=mpegts:muxdelay=0:muxpreload=0]{transport_stream_path}",
                f"[{':'.join(hls_options)}]{playlist}",
            ]
        )

        cmd = self._build_block_inputs(block, stream_profile=stream_profile)
        filter_complex = self._filter_complex(block, upload_to_vaapi=self.video_encoder.endswith("_vaapi"))
        cmd.extend(self._normalized_video_audio_output_args(filter_complex))
        if duration_limit is not None:
            cmd.extend(["-t", self._num(duration_limit)])
        if stream_profile == "jellyfin" and hls_start_time_offset is not None:
            cmd.extend(["-output_ts_offset", self._num(hls_start_time_offset)])
        cmd.extend(["-f", "tee", tee_output])
        return cmd

    def _build_block_inputs(self, block: PlannedBlock, *, stream_profile: StreamProfile) -> list[str]:
        cmd: list[str] = [self.ffmpeg, "-hide_banner", "-y"]
        if stream_profile == "jellyfin":
            cmd.extend(["-fflags", "+genpts"])
        if self.vaapi_device:
            cmd.extend(["-vaapi_device", self.vaapi_device])
        for item in block.items:
            if item.input_kind == "lavfi":
                if item.duration > 0:
                    cmd.extend(["-t", self._num(item.duration)])
                cmd.extend(["-re", "-f", "lavfi", "-i", item.ffmpeg_input or "color=black"])
                continue
            if item.input_kind == "image_loop":
                if item.duration > 0:
                    cmd.extend(["-loop", "1", "-re", "-t", self._num(item.duration), "-i", item.ffmpeg_input or str(item.resolved_path)])
                else:
                    cmd.extend(["-loop", "1", "-re", "-i", item.ffmpeg_input or str(item.resolved_path)])
                continue
            if item.skip > 0:
                cmd.extend(["-ss", self._num(item.skip)])
            if item.duration > 0:
                cmd.extend(["-t", self._num(item.duration)])
            cmd.extend(["-re", "-i", item.ffmpeg_input or str(item.resolved_path)])
        return cmd

    def _normalized_video_audio_output_args(self, filter_complex: str) -> list[str]:
        video_args = ["-c:v", self.video_encoder]
        if self.video_encoder.endswith("_vaapi"):
            video_args.extend(["-qp", "23", "-g", "50"])
        else:
            video_args.extend(["-preset", "veryfast", "-crf", "23", "-g", "50", "-keyint_min", "50", "-sc_threshold", "0"])
        return [
            "-filter_complex",
            filter_complex,
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            *video_args,
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
        ]

    @staticmethod
    def _filter_complex(block: PlannedBlock, *, upload_to_vaapi: bool = False) -> str:
        chains: list[str] = []
        labels: list[str] = []
        for idx, item in enumerate(block.items):
            chains.append(
                f"[{idx}:v]scale=640:480:force_original_aspect_ratio=decrease,"
                "pad=640:480:(ow-iw)/2:(oh-ih)/2,"
                "fps=25,setsar=1,format=yuv420p"
                f"[v{idx}]"
            )
            if item.probe.audio_channels > 0:
                chains.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo[a{idx}]")
            else:
                duration = item.duration or item.probe.duration or 1.0
                chains.append(
                    "anullsrc=channel_layout=stereo:sample_rate=48000,"
                    f"atrim=duration={FFMpegHLSCommandBuilder._num(duration)},"
                    "asetpts=PTS-STARTPTS"
                    f"[a{idx}]"
                )
            labels.append(f"[v{idx}][a{idx}]")
        concat_video = "vcat" if upload_to_vaapi else "vout"
        chains.append("".join(labels) + f"concat=n={len(block.items)}:v=1:a=1[{concat_video}][aout]")
        if upload_to_vaapi:
            chains.append("[vcat]format=nv12,hwupload[vout]")
        return ";".join(chains)

    @staticmethod
    def _slug(title: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")
        return slug or "block"

    @staticmethod
    def _num(value: float) -> str:
        return (f"{value:.6f}").rstrip("0").rstrip(".")

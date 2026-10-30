"""Translate NENE quality/shape into LTX's documented pixel resolutions.

https://docs.ltx.io/models/ltx-2-5#resolutions
Keep accounting in NENE's quality labels; convert only at the provider boundary.
"""
_RESOLUTIONS = {
    '720p': {'16:9':'1280x720', '9:16':'720x1280', '1:1':'720x720', '3:2':'1080x720', '2:3':'720x1080'},
    '1080p': {'16:9':'1920x1080', '9:16':'1080x1920', '1:1':'1080x1080', '3:2':'1620x1080', '2:3':'1080x1620'},
    '1440p': {'16:9':'2560x1440', '9:16':'1440x2560', '1:1':'1440x1440', '3:2':'2160x1440', '2:3':'1440x2160'},
    '4k': {'16:9':'3840x2160', '9:16':'2160x3840', '1:1':'2160x2160', '3:2':'3240x2160', '2:3':'2160x3240'},
}


def output_resolution(quality: str, aspect_ratio: str | None = None) -> str:
    try:
        return _RESOLUTIONS[quality][aspect_ratio or '16:9']
    except (KeyError, TypeError) as error:
        raise ValueError('Unsupported LTX resolution or aspect ratio.') from error

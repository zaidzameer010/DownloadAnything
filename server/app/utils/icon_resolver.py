import json
import os
import re
import subprocess
import sys
from app.utils.logger import logger

_icon_cache: dict[str, str] = {}


def get_system_icon_base64(path_or_ext: str) -> str | None:
    """
    Query the operating system's native API to retrieve the file type icon
    and return it as a base64 encoded PNG Data URL.
    """
    if sys.platform == "darwin":
        # macOS implementation using JavaScript for Automation (JFA) Cocoa access
        escaped_target = json.dumps(path_or_ext)
        js_code = f"""
        ObjC.import('AppKit');
        ObjC.import('Foundation');
        var ws = $.NSWorkspace.sharedWorkspace;
        var icon = null;
        var target = {escaped_target};
        
        if (target === "folder" || target === "directory" || target === "public.folder") {{
            icon = ws.iconForFileType('public.folder');
        }} else if (target.startsWith(".") || target.length <= 5) {{
            icon = ws.iconForFileType(target.replace(".", ""));
        }} else {{
            var fm = $.NSFileManager.defaultManager;
            if (fm.fileExistsAtPath(target)) {{
                icon = ws.iconForFile(target);
            }} else {{
                var parts = target.split('.');
                var ext = parts.length > 1 ? parts.pop() : '';
                if (ext) {{
                    icon = ws.iconForFileType(ext);
                }} else {{
                    icon = ws.iconForFileType('public.data');
                }}
            }}
        }}
        
        if (icon) {{
            var tiffData = icon.TIFFRepresentation;
            if (tiffData) {{
                var bitmap = $.NSBitmapImageRep.imageRepWithData(tiffData);
                var pngData = bitmap.representationUsingTypeProperties(4, $.NSDictionary.dictionary);
                if (pngData) {{
                    var base64String = pngData.base64EncodedStringWithOptions(0);
                    'data:image/png;base64,' + base64String.js;
                }}
            }}
        }}
        """
        try:
            proc = subprocess.run(
                ["osascript", "-l", "JavaScript", "-e", js_code],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            return proc.stdout.strip() or None
        except Exception as e:
            logger.debug(f"Failed to get macOS system icon: {e}")
            return None

    elif sys.platform == "win32":
        # Windows implementation using PowerShell to extract System.Drawing.Icon
        is_folder = path_or_ext in ("folder", "directory", "public.folder") or (
            not path_or_ext.startswith(".") and "." not in os.path.basename(path_or_ext)
        )

        if is_folder:
            ps_script = """
            Add-Type -AssemblyName System.Drawing
            $tempDir = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), [System.Guid]::NewGuid().ToString())
            New-Item -ItemType Directory -Path $tempDir | Out-Null
            try {
                $icon = [System.Drawing.Icon]::ExtractAssociatedIcon($tempDir)
                $ms = New-Object System.IO.MemoryStream
                $icon.ToBitmap().Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
                $bytes = $ms.ToArray()
                Write-Output ("data:image/png;base64," + [Convert]::ToBase64String($bytes))
            } finally {
                Remove-Item -Recurse -Force $tempDir | Out-Null
            }
            """
        else:
            ext = (
                path_or_ext
                if path_or_ext.startswith(".")
                else f".{path_or_ext.split('.')[-1]}"
            )
            if not re.fullmatch(r"\.[A-Za-z0-9]{1,16}", ext):
                ext = ".bin"
            ps_script = f"""
            Add-Type -AssemblyName System.Drawing
            $tempFile = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), [System.Guid]::NewGuid().ToString() + "{ext}")
            New-Item -ItemType File -Path $tempFile | Out-Null
            try {{
                $icon = [System.Drawing.Icon]::ExtractAssociatedIcon($tempFile)
                $ms = New-Object System.IO.MemoryStream
                $icon.ToBitmap().Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
                $bytes = $ms.ToArray()
                Write-Output ("data:image/png;base64," + [Convert]::ToBase64String($bytes))
            }} finally {{
                Remove-Item -Force $tempFile | Out-Null
            }}
            """
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            return proc.stdout.strip() or None
        except Exception as e:
            logger.debug(f"Failed to get Windows system icon: {e}")
            return None

    return None


def get_cached_system_icon(path_or_ext: str) -> str | None:
    """Gets the base64 icon data from cache, or queries the OS."""
    key = path_or_ext.lower().strip()
    if not key:
        return None
    if key in _icon_cache:
        return _icon_cache[key]

    val = get_system_icon_base64(key)
    if val:
        _icon_cache[key] = val
    return val


def enrich_job_with_icon(job_dict: dict[str, object]) -> dict[str, object]:
    """Appends system-based base64 thumbnails to job payloads if not already present."""
    thumbnail = job_dict.get("thumbnail")
    if isinstance(thumbnail, str) and thumbnail:
        return job_dict

    file_path_value = job_dict.get("file_path")
    file_path = file_path_value if isinstance(file_path_value, str) else None
    title_value = job_dict.get("title")
    title = title_value if isinstance(title_value, str) else ""
    url_value = job_dict.get("url")
    url = url_value if isinstance(url_value, str) else ""
    media_type = job_dict.get("media_type")

    target: str | None = None
    if media_type == "torrent" or url.lower().startswith("magnet:"):
        target = ".torrent"
    elif file_path and os.path.exists(file_path):
        target = file_path
    elif file_path:
        _, ext = os.path.splitext(file_path)
        target = ext if ext else "folder" if media_type == "torrent" else ".mp4"
    else:
        _, ext = os.path.splitext(title)
        if not ext:
            _, ext = os.path.splitext(url.split("?")[0])
        target = ext if ext else ".mp4"

    if target:
        icon_url = get_cached_system_icon(target)
        if icon_url:
            job_dict["thumbnail"] = icon_url

    return job_dict

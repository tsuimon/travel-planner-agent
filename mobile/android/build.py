"""Build a signed local-test APK using JDK 17 and official Android SDK tools.

Run with --setup to download the two pinned SDK archives into .tools/android.
No provider credentials, runtime databases or .env files enter the APK.
"""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
APP = Path(__file__).resolve().parent
SDK = ROOT / ".tools" / "android"
BUILD = APP / "build"
DIST = ROOT / "dist"
ARCHIVES = [
    ("platform-35_r02.zip", "0bb560a90a7a2cbd0dd8348224d518b638fe7949"),
    ("build-tools_r35_windows.zip", "af059bb67cf7786f45ee0db85e2d24985df1b4b6"),
]


def setup() -> None:
    """Download from Google's official repository and verify its published checksums."""
    SDK.mkdir(parents=True, exist_ok=True)
    for name, checksum in ARCHIVES:
        archive = SDK / name
        if not archive.exists() or hashlib.sha1(archive.read_bytes()).hexdigest() != checksum:
            print("Downloading", name, flush=True)
            with urllib.request.urlopen(
                "https://dl.google.com/android/repository/" + name, timeout=90
            ) as response:
                with archive.open("wb") as output:
                    shutil.copyfileobj(response, output)
        if hashlib.sha1(archive.read_bytes()).hexdigest() != checksum:
            raise RuntimeError("Official SDK checksum mismatch: " + name)
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                if not (SDK / member.filename).resolve().is_relative_to(SDK.resolve()):
                    raise RuntimeError("Unsafe SDK archive path")
            package.extractall(SDK)
        print("Verified and extracted", name, flush=True)


def run(*args: str | Path) -> None:
    subprocess.run([str(a) for a in args], check=True)


def build() -> Path:
    if os.name != "nt":
        raise RuntimeError("This pinned SDK bootstrap targets Windows.")
    java = shutil.which("java")
    javac = shutil.which("javac")
    if not java or not javac:
        raise RuntimeError("Install JDK 17+ and put java/javac on PATH.")
    keytool = shutil.which("keytool")
    if not keytool:
        properties = subprocess.run(
            [java, "-XshowSettings:properties", "-version"],
            check=True,
            capture_output=True,
            text=True,
            errors="replace",
        )
        java_home = next(
            line.split("=", 1)[1].strip()
            for line in properties.stderr.splitlines()
            if line.strip().startswith("java.home =")
        )
        keytool = str(Path(java_home) / "bin" / "keytool.exe")
    platform = next(SDK.glob("*/android.jar"), None)
    aapt = next(SDK.glob("*/aapt2.exe"), None)
    if not platform or not aapt:
        raise RuntimeError("SDK missing. Run: python mobile/android/build.py --setup")
    tools = aapt.parent
    BUILD.mkdir(parents=True, exist_ok=True)
    DIST.mkdir(parents=True, exist_ok=True)
    classes = BUILD / "classes"
    classes.mkdir(exist_ok=True)
    resources = BUILD / "resources.zip"
    run(aapt, "compile", "--dir", APP / "res", "-o", resources)
    unsigned = BUILD / "unsigned.apk"
    run(aapt, "link", "-o", unsigned, "--manifest", APP / "AndroidManifest.xml", "-I", platform, resources)
    source_files = sorted((APP / "src").rglob("*.java"))
    run(javac, "-encoding", "UTF-8", "--release", "8", "-classpath", platform, "-d", classes, *source_files)
    run(
        java,
        "-cp",
        tools / "lib/d8.jar",
        "com.android.tools.r8.D8",
        "--min-api",
        "26",
        "--lib",
        platform,
        "--output",
        BUILD,
        *sorted(classes.rglob("*.class")),
    )
    with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_DEFLATED) as package:
        package.write(BUILD / "classes.dex", "classes.dex")
    aligned = BUILD / "aligned.apk"
    run(tools / "zipalign.exe", "-f", "4", unsigned, aligned)
    keystore = SDK / "local-test.keystore"
    if not keystore.exists():
        run(
            keytool,
            "-genkeypair",
            "-keystore",
            keystore,
            "-storepass",
            "android",
            "-keypass",
            "android",
            "-alias",
            "androiddebugkey",
            "-keyalg",
            "RSA",
            "-keysize",
            "2048",
            "-validity",
            "3650",
            "-dname",
            "CN=Travel Planner Local Test,O=Local Development,C=CN",
        )
    apk = DIST / "travel-planner-0.1.0.apk"
    run(
        java,
        "-jar",
        tools / "lib/apksigner.jar",
        "sign",
        "--ks",
        keystore,
        "--ks-key-alias",
        "androiddebugkey",
        "--ks-pass",
        "pass:android",
        "--key-pass",
        "pass:android",
        "--out",
        apk,
        aligned,
    )
    run(java, "-jar", tools / "lib/apksigner.jar", "verify", "--verbose", apk)
    run(tools / "zipalign.exe", "-c", "4", apk)
    digest = hashlib.sha256(apk.read_bytes()).hexdigest()
    (DIST / (apk.name + ".sha256")).write_text(digest + "  " + apk.name + "\n", encoding="utf-8")
    print("APK:", apk, flush=True)
    print("SHA256:", digest, flush=True)
    return apk


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true", help="Download official SDK archives, about 124 MB")
    if parser.parse_args().setup:
        setup()
    build()

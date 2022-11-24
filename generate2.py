#!/usr/bin/env python3

import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tomli

def load_props(dir, name):
    path = os.path.join(dir, name + ".toml")
    if os.path.isfile(path):
        with open(path, "rb") as f:
            return tomli.load(f)
    else:
        return {}

def load_signature(apk_path):
    apksigner_output = subprocess.check_output(["apksigner", "verify", "--print-certs", "--verbose", apk_path])
    sig_hash = None
    for line in apksigner_output.split(b'\n'):
        split = re.split("^Signer #[0-9]+ certificate SHA-256 digest: ", line.decode())
        if (len(split) == 2):
            if (sig_hash is not None):
                # Intentionally don't support APKs that have more than one signer
                raise Exception(apk_path + " has more than one signer")
            sig_hash = split[1]

    if sig_hash is None:
        raise Exception("didn't find signature of " + apk_path)

    return sig_hash

assert subprocess.call("./compress-apks") == 0

packages_dir = "apps/packages"
packages = {}

for pkg_name in sorted(os.listdir(packages_dir)):
    pkg_container_path = os.path.join(packages_dir, pkg_name)
    common_props = load_props(pkg_container_path, "common-props")

    if os.path.isfile(os.path.join(pkg_container_path, "icon.webp")):
        common_props["iconType"] = "webp"

    pkg_signatures = common_props["signatures"]
    package_variants = {}

    for pkg_version in sorted(os.listdir(pkg_container_path)):
        pkg_path = os.path.join(pkg_container_path, pkg_version)
        if not os.path.isdir(pkg_path):
            continue

        print("processing " + pkg_name + "/" + pkg_version)
        pkg_props = {"versionCode": int(pkg_version), "apks": [], "apkHashes": [],
                     "apkSizes": [], "apkGzSizes": [], "apkBrSizes": []}

        base_apk_path = os.path.join(pkg_path, "base.apk")
        assert os.path.isfile(base_apk_path)

        base_apk_signature = load_signature(base_apk_path)
        if (base_apk_signature not in pkg_signatures):
            raise Exception("unknown signature of " + base_apk_path + ", SHA-256: " + base_apk_signature)

        badging = subprocess.check_output(["aapt2", "dump", "badging", base_apk_path])

        lines = badging.split(b"\n")

        for kv in shlex.split(lines[0].decode()):
            if kv.startswith("versionName"):
                pkg_props["versionName"] = kv.split("=")[1]
            elif kv.startswith("versionCode"):
                assert pkg_props["versionCode"] == int(kv.split("=")[1])
            elif kv.startswith("name"):
                assert pkg_name == kv.split("=")[1]

        for line in lines[1:-1]:
            kv = shlex.split(line.decode())
            if kv[0].startswith("application-label:"):
                pkg_props["label"] = kv[0].split(":")[1]
            elif kv[0].startswith("sdkVersion"):
                pkg_props["minSdk"] = int(kv[0].split(":")[1])
            elif kv[0].startswith("native-code"):
                abi = kv[1]
                assert abi in ["arm64-v8a", "x86_64", "armeabi-v7a", "x86"]
                assert pkg_props.get("abis") == None
                pkg_props["abis"] = [ abi ]

        assert pkg_props.get("minSdk") != None

        for key,value in load_props(pkg_path, "props").items():
            pkg_props[key] = value

        assert pkg_props["channel"] in ["alpha", "beta", "stable", "old"]

        pkg_msg = "channel: " + pkg_props["channel"] + ", minSdk: " + str(pkg_props["minSdk"])
        maxSdk = pkg_props.get("maxSdk")
        if maxSdk != None:
            pkg_msg += ", maxSdk: " + maxSdk
        abis = pkg_props.get("abis")
        if abis != None:
            pkg_msg += "\nabis: " + ", ".join(abis)
        staticDeps = pkg_props.get("staticDeps")
        if staticDeps != None:
            pkg_msg += "\nstaticDeps: " + ", ".join(staticDeps)
        deps = pkg_props.get("deps")
        if deps != None:
            pkg_msg += "\ndeps: " + ", ".join(deps)
        pkg_msg += "\n"
        print(pkg_msg)

        for apk_name in sorted(filter(lambda n: n.endswith(".apk"), os.listdir(pkg_path))):
            apk_path = os.path.join(pkg_path, apk_name)

            apk_gz_path = apk_path + ".gz"
            apk_br_path = apk_path + ".br"

            assert os.path.getmtime(apk_path) == os.path.getmtime(apk_gz_path)
            assert os.path.getmtime(apk_path) == os.path.getmtime(apk_br_path)

            apk_hash_path = apk_path + ".sha256"

            if os.path.isfile(apk_hash_path):
                with open(apk_hash_path, "r") as f:
                    apk_hash = f.read()
            else:
                print("processing " + apk_path)

                if (load_signature(apk_path) != base_apk_signature):
                    # all apk splits must have the same signature
                    raise Exception("signature mismatch, apk: " + apk_path)

                badging = subprocess.check_output(["aapt2", "dump", "badging", apk_path])
                lines = badging.split(b"\n")
                apk_version_code = None
                for kv in shlex.split(lines[0].decode()):
                    if kv.startswith("versionCode"):
                        assert apk_version_code == None
                        apk_version_code = int(kv.split("=")[1])
                    elif kv.startswith("name"):
                        assert pkg_name == kv.split("=")[1]
                # all apk splits must have the same version code
                assert pkg_props["versionCode"] == apk_version_code

                hash = hashlib.new("sha256")
                with open(apk_path, "rb") as f:
                    hash.update(f.read())
                apk_hash = hash.hexdigest()
                with open(apk_hash_path, "w") as f:
                    f.write(apk_hash)

            pkg_props["apkHashes"].append(apk_hash)
            pkg_props["apkSizes"].append(int(os.path.getsize(apk_path)))
            pkg_props["apkGzSizes"].append(int(os.path.getsize(apk_gz_path)))
            pkg_props["apkBrSizes"].append(int(os.path.getsize(apk_br_path)))
            pkg_props["apks"].append(apk_name)

        # "old" release channel is for previous version(s), to prevent clients from getting
        # 404 errors when updating packages
        if pkg_props["channel"] == "old":
            continue

        package_variants[pkg_version] = pkg_props

    common_props["variants"] = package_variants
    packages[pkg_name] = common_props

metadata = {
    "time": int(datetime.datetime.utcnow().timestamp()),
    "packages": packages
}

metadata_prefix = "apps/metadata.1"
metadata_json = metadata_prefix + ".json"
metadata_json_sig = metadata_json + ".0.sig"
metadata_sjson = metadata_prefix + ".sjson"

with open(metadata_json, "w") as f:
    json.dump(metadata, f, separators=(',', ':'))

subprocess.check_output(["signify", "-S", "-s", "apps.0.sec", "-m", metadata_json, "-x", metadata_json_sig])

with open(metadata_json_sig) as f:
    sig = f.read().splitlines()[1]

shutil.copy(metadata_json, metadata_sjson)

with open(metadata_sjson, "a") as f:
    f.write("\n" + sig + "\n")
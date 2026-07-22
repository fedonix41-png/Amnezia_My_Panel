import sys, base64, zlib, struct

with open("python_qr.txt") as f:
    py_raw = base64.urlsafe_b64decode(f.read().replace("vpn://", "") + "==")
    py_json = zlib.decompress(py_raw[12:])

with open("php_qr.txt") as f:
    php_raw = base64.urlsafe_b64decode(f.read().replace("vpn://", "") + "==")
    php_json = zlib.decompress(php_raw[12:])

if py_json == php_json:
    print("JSON IS IDENTICAL!")
else:
    print("JSON DIFFERS!")
    print("Python length:", len(py_json))
    print("PHP length:", len(php_json))
    # print diff
    for i in range(len(py_json)):
        if py_json[i] != php_json[i]:
            print(f"Diff at byte {i}: Py '{py_json[i:i+10]}' vs PHP '{php_json[i:i+10]}'")
            break

import sys, json, base64, zlib, struct
from managers.amnezia_config import build_amnezia_config

conf = """[Interface]
Address = 10.8.1.17/32
DNS = 1.1.1.1, 1.0.0.1
PrivateKey = K+RkrRJ2NlCzpvf6j2LFk5aQqCMCSqUdJ9LszIF+sS0=
MTU = 1280
Jc = 10
Jmin = 11
Jmax = 47
S1 = 13
S2 = 10
H1 = 2238079220
H2 = 1626497588
H3 = 2573613693
H4 = 291057490

[Peer]
PublicKey = ObM16FWdjyFbdGauX4IeE1Tr8S9l2SJW0WZqcd2U6zA=
PresharedKey = xJhAq+BPrbnJ8Sf6aSur202Yk9rOUMieQ6QlroGtsAs=
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = 45.61.148.113:55444
PersistentKeepalive = 25"""

config = build_amnezia_config(conf, "awg")
print(json.dumps(config, indent=4, ensure_ascii=False))

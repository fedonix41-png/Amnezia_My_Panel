
import os
import logging

logger = logging.getLogger(__name__)

class DNSManager:
    def __init__(self, ssh):
        self.ssh = ssh

    def install_protocol(self, protocol_type='dns', port='53'):
        port = int(port) if port else 53
        """Install AmneziaDNS service."""
        try:
            # 1. Check if docker is installed
            out, _, _ = self.ssh.run_command("docker --version")
            if "docker version" not in out.lower():
                return {"status": "error", "message": "Docker not installed"}

            # 2. Prepare directory
            self.ssh.run_sudo_command("mkdir -p /opt/amnezia/dns")

            # 3. Create forward-records.conf matching official Amnezia implementation
            # We use Unbound (mvance/unbound:latest) with DNS-over-TLS to Cloudflare
            forward_config = """server:
    tls-cert-bundle: /etc/ssl/certs/ca-certificates.crt

forward-zone:
    name: "."
    forward-tls-upstream: yes
    forward-addr: 1.1.1.1@853
    forward-addr: 1.0.0.1@853
"""
            self.ssh.write_file("/opt/amnezia/dns/forward-records.conf", forward_config)

            # 4. Stop existing container if any
            self.ssh.run_sudo_command("docker stop amnezia-dns || true")
            self.ssh.run_sudo_command("docker rm amnezia-dns || true")

            # Verify and recreate amnezia-dns-net with exact subnet 172.29.172.0/24
            net_inspect, _, _ = self.ssh.run_sudo_command("docker network inspect amnezia-dns-net 2>/dev/null")
            if "172.29.172.0/24" not in net_inspect:
                # Disconnect any endpoints attached to old amnezia-dns-net network so removal succeeds
                self.ssh.run_sudo_command(
                    "for c in $(docker network inspect amnezia-dns-net -f '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null); do "
                    "docker network disconnect -f amnezia-dns-net $c || true; done"
                )
                self.ssh.run_sudo_command("docker network rm amnezia-dns-net 2>/dev/null || true")
                out_net, err_net, code_net = self.ssh.run_sudo_command("docker network create --subnet 172.29.172.0/24 amnezia-dns-net")
                if code_net != 0:
                    return {"status": "error", "message": f"Failed to create network amnezia-dns-net: {err_net or out_net}"}

            # Pre-pull image to avoid docker run timeout
            self.ssh.run_sudo_command("docker pull mvance/unbound:latest")

            # Use internal network with static IP and volume mount config
            cmd = (
                "docker run -d --name amnezia-dns --restart always "
                "--network amnezia-dns-net --ip=172.29.172.254 "
                "-v /opt/amnezia/dns/forward-records.conf:/opt/unbound/etc/unbound/forward-records.conf:ro "
                "mvance/unbound:latest"
            )
            out, err, code = self.ssh.run_sudo_command(cmd)
            if code != 0:
                return {"status": "error", "message": err or out or "Failed to run amnezia-dns container"}

            # Connect existing VPN containers to the DNS network
            vpn_containers = ['amnezia-awg', 'amnezia-awg2', 'amnezia-awg-legacy', 'amnezia-xray', 'telemt']
            for c in vpn_containers:
                self.ssh.run_sudo_command(f"docker ps -q -f name=^{c}$ | grep -q . && docker network connect amnezia-dns-net {c} || true")

            return {"status": "success", "message": "AmneziaDNS installed successfully"}
        except Exception as e:
            logger.exception("Error installing DNS")
            return {"status": "error", "message": str(e)}


    def get_server_status(self, protocol_type='dns'):
        """Check if AmneziaDNS container is running."""
        try:
            out, _, _ = self.ssh.run_sudo_command("docker ps --filter name=^amnezia-dns$ --format '{{.Status}}'")
            is_running = 'Up' in out
            
            out_exists, _, _ = self.ssh.run_sudo_command("docker ps -a --filter name=^amnezia-dns$ --format '{{.Names}}'")
            container_exists = 'amnezia-dns' in out_exists.strip().split('\n')
            
            return {
                "container_exists": container_exists,
                "container_running": is_running,
                "port": "53",
                "protocol": protocol_type
            }
        except Exception as e:
            return {"error": str(e)}

    def remove_container(self, protocol_type='dns'):
        """Remove AmneziaDNS container."""
        self.ssh.run_sudo_command("docker stop amnezia-dns || true")
        self.ssh.run_sudo_command("docker rm amnezia-dns || true")
        self.ssh.run_sudo_command("docker network rm amnezia-dns-net || true")
        self.ssh.run_sudo_command("rm -rf /opt/amnezia/dns")


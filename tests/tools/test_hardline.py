import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from niya.tools.hardline import default_hardline, detect_hardline_command

CASES = json.loads((Path(__file__).parent / "data" / "hermes_hardline_cases.json").read_text(encoding="utf-8"))


class HermesCasesTests(unittest.TestCase):
    def test_hermes_block_cases(self):
        for command in CASES["block"]:
            with self.subTest(command=command):
                self.assertTrue(detect_hardline_command(command)[0])

    def test_hermes_allow_cases(self):
        for command in CASES["allow"]:
            with self.subTest(command=command):
                self.assertFalse(detect_hardline_command(command)[0])


class NiyaRulesTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"HOME": "/root"}))
        self.enterContext(patch("os.path.expanduser", side_effect=lambda p: p.replace("~", "/root", 1)))
        self.hardline = default_hardline([Path("/opt/niya"), Path("/var/lib/niya"), Path("/etc/niya")])

    def test_blocks_root_specific_rules(self):
        for command in [
            "systemctl stop niya",
            "sudo systemctl disable --now niya.service",
            "systemctl mask niya",
            "pkill -f niya",
            "killall niya",
            "kill -9 $(pgrep -f niya)",
            "rm -rf /var/lib/niya",
            "rm /var/lib/niya/niya.db",
            'rm -rf "/opt/niya"',
            "rm -rf /tmp/x /etc/niya",
            "truncate -s 0 /var/lib/niya/niya.db",
            "> /var/lib/niya/niya.db",
            "systemctl stop sshd",
            "systemctl disable ssh.socket",
            "service ssh stop",
            "iptables -F",
            "iptables -t filter --flush",
            "iptables -P INPUT DROP",
            "ip6tables -P INPUT REJECT",
            "nft flush ruleset",
            "rm ~/.ssh/authorized_keys",
            "rm -f /root/.ssh/authorized_keys",
            ": > ~/.ssh/authorized_keys",
            "cp /dev/null /etc/ssh/sshd_config",
            "rm /etc/ssh/sshd_config",
            "passwd -l root",
            "usermod -L root",
            "echo ok && systemctl stop niya",
        ]:
            with self.subTest(command=command):
                self.assertIsNotNone(self.hardline.check(command))

    def test_allows_normal_administration(self):
        for command in [
            "systemctl restart nginx",
            "systemctl restart sshd",
            "systemctl status niya",
            "journalctl -u niya -n 50",
            "apt install -y jq",
            "sed -i.bak 's/80/8080/' /etc/nginx/sites-available/default",
            "cat ~/.ssh/authorized_keys",
            "echo 'ssh-ed25519 AAAA' >> ~/.ssh/authorized_keys",
            "cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak",
            "sqlite3 /var/lib/niya/niya.db 'select count(*) from messages'",
            "ls /var/lib/niya",
            "iptables -L -n",
            "iptables -A INPUT -p tcp --dport 443 -j ACCEPT",
            "git commit -m 'do not systemctl stop niya'",
            "grep 'rm -rf /var/lib/niya' notes.txt",
            "rm -rf /tmp/niya-build",
        ]:
            with self.subTest(command=command):
                self.assertIsNone(self.hardline.check(command))

    def test_floor_still_applies(self):
        self.assertIsNotNone(self.hardline.check("rm -rf /"))
        self.assertIsNotNone(self.hardline.check("r''m -rf /"))


if __name__ == "__main__":
    unittest.main()

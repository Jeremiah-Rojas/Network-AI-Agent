#!/usr/bin/env python3
"""
gns3_cisco_ai_agent.py

Autonomous AI agent for GNS3 lab automation.

WHAT THIS DOES
--------------
Runs on the GNS3 host (Linux). Continuously prompts you, in plain English, for a
networking task ("Configure an ACL to deny 192.168.0.33 from passing this
device", "Create VLAN 20 named GUEST and assign Gi0/1 to it", etc). It sends the
task to Google Gemini, which returns the exact Cisco IOS commands needed. The
agent then opens/reuses an SSH session (via Netmiko) to a Cisco IOSv (router)
or IOSvL2 (Layer-2 switch) node in your GNS3 topology, runs the commands with
correct pacing, prints all device output to the screen, automatically saves
the configuration, and if anything goes wrong it diagnoses the failure and
proposes (and, where safe, automatically applies) a fix.

If Gemini needs more information to write a correct command (e.g. which
interface/sub-interface, which VLAN, in/out direction, etc.) it will ask you
a follow-up question instead of guessing.

Type "end" at the task prompt to quit the program.
Type "switch" at the task prompt to disconnect and pick a different device.

REQUIREMENTS
------------
    pip install netmiko google-genai

SETUP -- READ BEFORE RUNNING
-----------------------------
1) Enter your Gemini API key below in the CONFIG section
   (GEMINI_API_KEY = "..."), or export it as an environment variable named
   GEMINI_API_KEY before launching this script.
2) Fill in the management IP address, username, password, and (if set) enable
   secret for your IOSv and IOSvL2 nodes in DEVICE_PROFILES below. These are
   the SSH credentials you already configured on the routers/switches inside
   GNS3 (line vty / local user / enable secret / "ip ssh version 2").
3) Make sure the GNS3 node has a management interface reachable from this
   Linux host (e.g. bridged to a Cloud/NAT node) and that SSH is already
   enabled on the device (crypto keys generated, vty transport input ssh).

Everything this program prints uses the terminal's normal default text
color -- no colored/mixed output.
"""

import os
import re
import sys
import time

try:
    from netmiko import ConnectHandler
    from netmiko.exceptions import (
        NetMikoTimeoutException,
        NetMikoAuthenticationException,
        SSHException,
    )
except ImportError:
    print("Missing dependency 'netmiko'. Install it with:\n    pip install netmiko")
    sys.exit(1)

try:
    from google import genai
    from google.genai import types
except ImportError:
    print("Missing dependency 'google-genai'. Install it with:\n    pip install google-genai")
    sys.exit(1)


# =============================================================================
# CONFIG -- EDIT THIS SECTION
# =============================================================================

# ---- Gemini API key -----------------------------------------------------
# >>> ENTER YOUR GEMINI API KEY HERE <<<
# You can either paste it directly on the line below, or leave the string as
# "YOUR_GEMINI_API_KEY_HERE" and instead set an environment variable before
# running the script:   export GEMINI_API_KEY="your-key-here"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "API_KEY_HERE")

# Model used as the agent's "brain". gemini-2.5-flash is fast and inexpensive
# and is more than capable of writing IOS config; swap in another Gemini
# model name here if you prefer (e.g. a newer/heavier reasoning model).
GEMINI_MODEL = "gemini-2.5-flash"

# ---- Device profiles -----------------------------------------------------
# Fill in the real management IP / credentials for the two GNS3 nodes.
# device_type "cisco_ios" is correct for both IOSv and IOSvL2.
DEVICE_PROFILES = {
    "iosv": {
        "label": "Cisco IOSv (router)",
        "device_type": "cisco_ios",
        "host": "192.168.x.x",          # <-- IOSv management IP
        "username": "msfadmin",             # <-- SSH username
        "password": "msfadmin",             # <-- SSH password
        "secret": "",               # <-- enable secret (leave "" if none)
        "port": 22,
    },
    "iosvl2": {
        "label": "Cisco IOSvL2 (Layer-2 switch)",
        "device_type": "cisco_ios",
        "host": "192.168.x.x",          # <-- IOSvL2 management IP
        "username": "msfadmin",             # <-- SSH username
        "password": "msfadmin",             # <-- SSH password
        "secret": "",               # <-- enable secret (leave "" if none)
        "port": 22,
    },
}

# GNS3 serial/vNIC-backed consoles can be slow -- these give Netmiko more
# patience so commands aren't cut off. Raise GLOBAL_DELAY_FACTOR if you see
# truncated output.
GLOBAL_DELAY_FACTOR = 2
CONN_TIMEOUT = 25

# Safety limits for the autonomous loops.
MAX_CLARIFICATION_ROUNDS = 4
MAX_AUTO_REMEDIATION_ATTEMPTS = 2

# =============================================================================
# END CONFIG
# =============================================================================


IOS_ERROR_PATTERNS = [
    r"% Invalid input",
    r"% Incomplete command",
    r"% Ambiguous command",
    r"% Unrecognized command",
    r"% Unknown command",
    r"% Bad ",
    r"% Configuration failed",
    r"Command rejected",
    r"Error:",
]

PLACEHOLDER_HOST = "192.168.x.x"


# -----------------------------------------------------------------------------
# Small helpers (all output uses plain default terminal color, consistently)
# -----------------------------------------------------------------------------

def banner(text):
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78)


def section(text):
    print("\n" + "-" * 78)
    print(text)
    print("-" * 78)


def ask(prompt_text):
    try:
        return input(prompt_text)
    except EOFError:
        return "end"


# -----------------------------------------------------------------------------
# Gemini setup
# -----------------------------------------------------------------------------

def get_gemini_client():
    if not GEMINI_API_KEY or GEMINI_API_KEY == "YOUR_GEMINI_API_KEY_HERE":
        print("ERROR: No Gemini API key configured.")
        print("Open this script and set GEMINI_API_KEY in the CONFIG section,")
        print("or export it as an environment variable:  export GEMINI_API_KEY=\"...\"")
        sys.exit(1)
    return genai.Client(api_key=GEMINI_API_KEY)


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "needs_clarification": {"type": "boolean"},
        "clarification_question": {"type": "string"},
        "explanation": {"type": "string"},
        "config_commands": {"type": "array", "items": {"type": "string"}},
        "verify_commands": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "needs_clarification",
        "clarification_question",
        "explanation",
        "config_commands",
        "verify_commands",
    ],
}

REMEDIATION_SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string"},
        "fix_commands": {"type": "array", "items": {"type": "string"}},
        "explanation": {"type": "string"},
        "needs_physical_check": {"type": "boolean"},
    },
    "required": ["root_cause", "fix_commands", "explanation", "needs_physical_check"],
}


def plan_system_instruction(device_label):
    return f"""You are an expert Cisco IOS network engineer acting as the reasoning
core of an autonomous network-automation agent. The agent has a live SSH
session open to a real lab device and will execute, verbatim, whatever
commands you return, so every command must be syntactically correct and
directly runnable.

Device under management right now: {device_label}

Rules:
1. Respond only with the structured JSON fields defined by the response
   schema. No markdown fences, no commentary outside those fields.
2. If information you cannot safely infer is missing (an exact interface or
   sub-interface name, an IP address/mask, ACL permit/deny direction, VLAN
   number, hostname, etc.), set needs_clarification to true, ask exactly one
   precise question in clarification_question, and leave both command lists
   empty.
3. Once you have everything needed, set needs_clarification to false and
   provide:
   - config_commands: an ordered list of Cisco IOS configuration-mode
     commands that accomplish the task. Do NOT include "configure terminal",
     "end", "write memory", or "copy running-config startup-config" -- the
     calling program adds those automatically. DO include any "exit"
     commands needed to move between configuration sub-modes (for example,
     leaving an access-list context before entering an interface context).
   - verify_commands: an ordered list of exec-mode read-only commands (e.g.
     "show ..." commands) that confirm the change was applied correctly.
   - explanation: 1-3 plain-English sentences describing what the commands
     do.
4. Never include comments, placeholders, or pseudo-code -- every command must
   use concrete, real values.
5. Match syntax to the platform. IOSv is a routed Cisco IOS device
   (interfaces, sub-interfaces, routing protocols, routed ACLs, NAT, etc.).
   IOSvL2 is a Layer-2 Cisco IOS switch (VLANs, switchports, trunks,
   spanning-tree, SVIs) -- do not suggest router-only features on it unless
   the user is clearly configuring an SVI or management interface.
6. Keep the configuration minimal and targeted to exactly what the user
   asked for; do not make unrelated changes."""


def remediation_system_instruction(device_label):
    return f"""You are an expert Cisco IOS troubleshooter. The automation agent just
sent a command to a live {device_label} over an active SSH session and the
device returned an error. Diagnose the likely root cause and, if possible,
provide corrected/remediation IOS commands.

Respond only with the structured JSON fields defined by the response schema.
- root_cause: concise explanation of why the command likely failed.
- fix_commands: ordered list of IOS configuration-mode commands (without
  "configure terminal"/"end"/"write memory") that would resolve the issue
  and still accomplish the original intent. Return an empty list if this is
  not something more commands can fix.
- needs_physical_check: true if this failure looks like it requires a human
  to check something outside the CLI (cabling/links in the GNS3 topology,
  node powered off, interface administratively down, wrong VLAN/port
  assignment, licensing, hardware limits, etc.), false otherwise.
- explanation: 1-3 plain-English sentences."""


def call_gemini_json(client, system_instruction, user_content, schema):
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        response_mime_type="application/json",
        response_schema=schema,
        temperature=0.2,
    )
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=user_content,
        config=config,
    )
    import json
    return json.loads(response.text)


def get_plan(client, device_label, task_text, clarification_transcript):
    user_content = f"User task: {task_text}"
    if clarification_transcript:
        user_content += "\n\nFollow-up clarification so far:\n" + clarification_transcript
    try:
        return call_gemini_json(
            client, plan_system_instruction(device_label), user_content, PLAN_SCHEMA
        )
    except Exception as e:
        print(f"[AGENT] Gemini request failed: {e}")
        return None


def get_remediation(client, device_label, failed_commands, device_output):
    user_content = (
        "Commands sent:\n"
        + "\n".join(failed_commands)
        + "\n\nDevice output/error:\n"
        + device_output
    )
    try:
        return call_gemini_json(
            client,
            remediation_system_instruction(device_label),
            user_content,
            REMEDIATION_SCHEMA,
        )
    except Exception as e:
        print(f"[AGENT] Gemini remediation request failed: {e}")
        return None


# -----------------------------------------------------------------------------
# Device connection handling
# -----------------------------------------------------------------------------

CONNECTION_TIPS = {
    "timeout": [
        "The SSH session could not reach the device (timeout). Possible causes:",
        "  - The GNS3 node isn't fully booted yet -- IOSv/IOSvL2 can take a minute or two.",
        "  - There's no cable/link between this node and your management network in the",
        "    GNS3 topology canvas, or the link is connected to the wrong port.",
        "  - The management interface on the device doesn't have the IP address you",
        "    configured here, or it's administratively down ('shutdown').",
        "  - The Linux host's route to the device's management subnet is missing, or a",
        "    Cloud/NAT node in GNS3 isn't bridged to the right host NIC.",
        "  - A firewall/ACL on the path is blocking TCP/22.",
        "Suggested checks: open the device console in GNS3 and run 'show ip interface",
        "brief' to confirm the management interface is up/up with the expected IP, and",
        "try 'ping <this-host-ip>' from the device itself.",
    ],
    "auth": [
        "The SSH session reached the device but authentication failed. Possible causes:",
        "  - The username/password in DEVICE_PROFILES doesn't match what's configured",
        "    on the device ('username <name> secret <pass>' or the vty login password).",
        "  - Local login isn't enabled on the vty lines ('line vty 0 4' / 'login local').",
        "  - AAA is configured and pointing somewhere other than the local database.",
        "Suggested fix commands to run from the device console:",
        "    configure terminal",
        "    username admin privilege 15 secret cisco",
        "    line vty 0 4",
        "    login local",
        "    transport input ssh",
        "    end",
        "    write memory",
    ],
    "ssh_not_ready": [
        "SSH itself appears unavailable on the device (connection refused / protocol",
        "error). Possible causes:",
        "  - SSH was never enabled (no RSA keys generated).",
        "  - No domain name is set, which is required before 'crypto key generate rsa'.",
        "Suggested fix commands to run from the device console:",
        "    configure terminal",
        "    ip domain-name lab.local",
        "    crypto key generate rsa modulus 2048",
        "    ip ssh version 2",
        "    line vty 0 4",
        "    transport input ssh",
        "    end",
        "    write memory",
    ],
    "generic": [
        "An unexpected error occurred while connecting. Double check the host IP, port,",
        "and that nothing else (like a console session already attached) is blocking",
        "the SSH connection, then try again.",
    ],
}


def print_tips(key):
    for line in CONNECTION_TIPS[key]:
        print(line)


def connect_to_device(device_key):
    profile = DEVICE_PROFILES[device_key]

    host = profile["host"]
    if host == PLACEHOLDER_HOST:
        host = ask(
            f"No management IP is set for {profile['label']} in the script. "
            f"Enter it now: "
        ).strip()

    params = {
        "device_type": profile["device_type"],
        "host": host,
        "username": profile["username"],
        "password": profile["password"],
        "port": profile.get("port", 22),
        "timeout": CONN_TIMEOUT,
        "global_delay_factor": GLOBAL_DELAY_FACTOR,
        "fast_cli": False,
    }
    if profile.get("secret"):
        params["secret"] = profile["secret"]

    print(f"\n[AGENT] Connecting to {profile['label']} at {host} ...")
    try:
        net_connect = ConnectHandler(**params)
        if profile.get("secret"):
            net_connect.enable()
        print(f"[AGENT] Connected. Session prompt: {net_connect.find_prompt()}")
        return net_connect
    except NetMikoTimeoutException:
        section("CONNECTION ERROR: host not reachable")
        print_tips("timeout")
    except NetMikoAuthenticationException:
        section("CONNECTION ERROR: authentication failed")
        print_tips("auth")
    except SSHException as e:
        section(f"CONNECTION ERROR: SSH problem ({e})")
        print_tips("ssh_not_ready")
    except Exception as e:
        section(f"CONNECTION ERROR: {e}")
        print_tips("generic")
    return None


def find_ios_errors(output):
    if not output:
        return False
    for pattern in IOS_ERROR_PATTERNS:
        if re.search(pattern, output):
            return True
    return False


# -----------------------------------------------------------------------------
# Task execution
# -----------------------------------------------------------------------------

def run_task(client, net_connect, device_label, task_text):
    clarification_transcript = ""
    plan = None

    for _ in range(MAX_CLARIFICATION_ROUNDS):
        plan = get_plan(client, device_label, task_text, clarification_transcript)
        if plan is None:
            print("[AGENT] Could not get a plan from Gemini. Aborting this task.")
            return
        if plan.get("needs_clarification"):
            question = plan.get("clarification_question", "").strip()
            print(f"\n[AGENT] I need more information: {question}")
            answer = ask("> ").strip()
            if answer.lower() == "end":
                print("[AGENT] Cancelling this task.")
                return
            clarification_transcript += f"\nQ: {question}\nA: {answer}"
            continue
        break
    else:
        print("[AGENT] Too many clarification rounds without enough detail. "
              "Please rephrase the task with more specifics.")
        return

    config_commands = plan.get("config_commands", [])
    verify_commands = plan.get("verify_commands", [])
    explanation = plan.get("explanation", "")

    section("PLAN")
    if explanation:
        print(explanation)
    if config_commands:
        print("\nConfiguration commands to run:")
        for cmd in config_commands:
            print(f"  {cmd}")
    if verify_commands:
        print("\nVerification commands to run afterward:")
        for cmd in verify_commands:
            print(f"  {cmd}")

    if not config_commands:
        print("[AGENT] No configuration changes to make for this task.")
        return

    # --- Apply configuration -------------------------------------------------
    section("APPLYING CONFIGURATION")
    output = net_connect.send_config_set(
        config_commands, delay_factor=GLOBAL_DELAY_FACTOR
    )
    print(output)

    attempted_commands = list(config_commands)
    attempts = 0
    while find_ios_errors(output) and attempts < MAX_AUTO_REMEDIATION_ATTEMPTS:
        attempts += 1
        section(f"ERROR DETECTED -- TROUBLESHOOTING (attempt {attempts})")
        remediation = get_remediation(client, device_label, attempted_commands, output)
        if remediation is None:
            print("[AGENT] Could not get troubleshooting help from Gemini. "
                  "Please review the output above manually.")
            break

        print(f"Likely root cause: {remediation.get('root_cause', 'unknown')}")
        print(remediation.get("explanation", ""))

        if remediation.get("needs_physical_check"):
            print("\n[AGENT] This looks like it may need a manual/physical check "
                  "(e.g. verify the link/cable in the GNS3 topology, confirm the "
                  "node is powered on, or confirm the interface/VLAN assignment) "
                  "rather than another command.")

        fix_commands = remediation.get("fix_commands", [])
        if fix_commands:
            print("\nAttempting automatic remediation with:")
            for cmd in fix_commands:
                print(f"  {cmd}")
            output = net_connect.send_config_set(
                fix_commands, delay_factor=GLOBAL_DELAY_FACTOR
            )
            print("\nDevice response:")
            print(output)
            attempted_commands = fix_commands
        else:
            print("[AGENT] No safe automatic fix available -- stopping auto-remediation.")
            break

    # --- Verification ---------------------------------------------------------
    if verify_commands:
        section("VERIFICATION OUTPUT")
        for cmd in verify_commands:
            result = net_connect.send_command(cmd, delay_factor=GLOBAL_DELAY_FACTOR)
            print(f"\n{device_label} # {cmd}")
            print(result)
            time.sleep(0.5)

    # --- Save configuration -----------------------------------------------------
    section("SAVING CONFIGURATION")
    try:
        save_output = net_connect.save_config()
        print(save_output if save_output else "Configuration saved (write memory).")
    except Exception as e:
        print(f"[AGENT] Could not automatically save configuration: {e}")
        print("Try running 'write memory' manually on the device.")


# -----------------------------------------------------------------------------
# Main loop
# -----------------------------------------------------------------------------

def choose_device():
    while True:
        print("\nWhich device would you like to SSH into?")
        print("  1) IOSv    - Cisco IOSv router")
        print("  2) IOSvL2  - Cisco IOSvL2 switch")
        print("  end        - quit the program")
        choice = ask("> ").strip().lower()
        if choice in ("end", "quit", "exit"):
            return None
        if choice in ("1", "iosv"):
            return "iosv"
        if choice in ("2", "iosvl2"):
            return "iosvl2"
        print("Please enter 1, 2, or 'end'.")


def main():
    banner("GNS3 Cisco IOS Autonomous Agent (powered by Google Gemini)")
    print("Type a networking task in plain English at the prompt below.")
    print("Type 'end' at any task prompt to quit. Type 'switch' to change devices.")

    client = get_gemini_client()

    while True:
        device_key = choose_device()
        if device_key is None:
            break

        net_connect = connect_to_device(device_key)
        if net_connect is None:
            retry = ask("\nTry a different device or connection? (yes/no): ").strip().lower()
            if retry.startswith("y"):
                continue
            else:
                break

        device_label = DEVICE_PROFILES[device_key]["label"]

        try:
            while True:
                section(f"Connected to {device_label}")
                task_text = ask("Enter task ('end' to quit, 'switch' to change device): ").strip()

                if not task_text:
                    continue
                if task_text.lower() == "end":
                    net_connect.disconnect()
                    print("\n[AGENT] Session closed. Goodbye.")
                    return
                if task_text.lower() == "switch":
                    net_connect.disconnect()
                    break

                run_task(client, net_connect, device_label, task_text)
        except KeyboardInterrupt:
            print("\n[AGENT] Interrupted by user. Closing SSH session...")
            try:
                net_connect.disconnect()
            except Exception:
                pass
            return
        except Exception as e:
            section(f"UNEXPECTED ERROR: {e}")
            print("The SSH session may have dropped. You'll be returned to device selection.")
            try:
                net_connect.disconnect()
            except Exception:
                pass

    print("\n[AGENT] Goodbye.")


if __name__ == "__main__":
    main()

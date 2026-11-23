"""Talk to the agent from a terminal (handy on the Sprite to test the real model + connectors):
    python3 -m upscore.chat "my chase limit is 2000, balance 1450, due the 28th"
    python3 -m upscore.chat            # interactive
"""
import sys

from . import agent

if len(sys.argv) > 1:
    print(agent.respond("cli", " ".join(sys.argv[1:]), "cli"))
else:
    while (line := input("you> ").strip()) not in ("", "exit", "quit"):
        print("upscore>", agent.respond("cli", line, "cli"))

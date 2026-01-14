"""
pygserver.scripting - Python NPC scripting support

This module provides the Python scripting interface for NPCs.

Example NPC Script:
-------------------

```python
class GreeterNPC:
    '''An NPC that greets players and moves on a timer.'''

    def on_created(self, npc):
        '''Called when the NPC is created.'''
        npc.image = "pics1.png"
        npc.say("Hello! I am a greeter NPC.")
        npc.set_timer(5.0)  # 5 second timer

    def on_timeout(self, npc):
        '''Called when the timer expires.'''
        npc.move(1, 0)  # Move right
        npc.set_timer(5.0)  # Reset timer

    def on_player_enters(self, npc, player):
        '''Called when a player enters the level.'''
        npc.say(f"Welcome, {player.nickname}!")

    def on_player_chats(self, npc, player, message):
        '''Called when a player sends a chat message.'''
        if "hello" in message.lower():
            npc.say(f"Hello to you too, {player.nickname}!")

    def on_player_touches(self, npc, player):
        '''Called when a player touches the NPC.'''
        npc.say("Hey, don't push me!")
```

Script Events:
--------------
- on_created(npc) - NPC initialized
- on_timeout(npc) - Timer expired
- on_player_enters(npc, player) - Player entered level
- on_player_leaves(npc, player) - Player left level
- on_player_chats(npc, player, message) - Player sent chat
- on_player_touches(npc, player) - Player touched NPC

NPC API:
--------
- npc.x, npc.y - Position (tiles)
- npc.direction - Facing direction (0-3)
- npc.image - NPC sprite image
- npc.gani - Animation name
- npc.message - Chat message above NPC
- npc.flags - Custom flags dict
- npc.move(dx, dy) - Move by offset
- npc.warp(level, x, y) - Warp to location
- npc.set_timer(seconds) - Set timeout timer
- npc.say(text) - Display message
- npc.hide() / npc.show() - Visibility
- npc.destroy() - Remove NPC
"""

from ..npc import NPC, NPCApi, NPCManager

__all__ = ["NPC", "NPCApi", "NPCManager"]

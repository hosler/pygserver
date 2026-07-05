"""
pygserver.account - Account system for player persistence

Handles account creation, authentication, and data storage.
Based on GServer-v2 TAccount implementation.
"""

import asyncio
import logging
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, List, Dict, Any

if TYPE_CHECKING:
    from .server import GameServer
    from .player import Player

logger = logging.getLogger(__name__)


@dataclass
class Account:
    """
    Represents a player account with all persistent data.
    """
    # Identity
    account_name: str
    password_hash: str = ""

    # Status
    is_banned: bool = False
    ban_reason: str = ""
    is_staff: bool = False
    admin_rights: int = 0

    # Stats
    kills: int = 0
    deaths: int = 0
    online_time: int = 0  # Seconds

    # Equipment
    head_image: str = "head19.png"
    body_image: str = "body.png"
    sword_image: str = "sword1.png"
    shield_image: str = "shield1.png"

    # Colors (skin, coat, sleeve, shoe, belt)
    colors: List[int] = field(default_factory=lambda: [0, 0, 0, 0, 0])

    # Stats
    max_hearts: float = 3.0
    hearts: float = 3.0
    rupees: int = 0
    # GServer-v2 default new-character stats (server/include/object/Character.h:
    # "uint8_t bombs = 10; uint8_t arrows = 5;"). Fresh accounts with 0 starting
    # bombs/arrows can never fire PLI_BOMBADD/PLI_ARROWADD, which silently
    # no-ops the whole bomb/arrow path.
    bombs: int = 10
    arrows: int = 5
    glove_power: int = 0
    sword_power: int = 1
    shield_power: int = 1
    mp: int = 0        # PLPROP_MAGICPOINTS
    ap: int = 50       # PLPROP_ALIGNMENT (GServer default 50)

    # Position
    level_name: str = ""
    x: float = 30.0
    y: float = 30.0

    # Flags (persistent player flags)
    flags: Dict[str, str] = field(default_factory=dict)

    # Gattribs (30 custom string attributes)
    gattribs: List[str] = field(default_factory=lambda: [""] * 30)

    # Weapons
    weapons: List[str] = field(default_factory=list)

    # Opened chests (list of chest IDs)
    chests_opened: List[str] = field(default_factory=list)

    # Admin comments
    comments: str = ""

    # Guild
    guild_name: str = ""
    guild_nickname: str = ""

    # Webpage profile (PLI_PROFILEGET/PROFILESET, PLO_PROFILE). GServer-v2
    # itself only transports these as an opaque blob forwarded to/from the
    # list server's SVO_GETPROF/SVO_SETPROF (ServerList.cpp msgSVI_PROFILE);
    # pygserver has no such external profile service, so these are stored
    # locally per-account instead (see ProfileManager below).
    profile_name: str = ""
    profile_age: str = ""
    profile_gender: str = ""
    profile_country: str = ""
    profile_messenger: str = ""
    profile_email: str = ""
    profile_website: str = ""
    profile_hangout: str = ""
    profile_quote: str = ""

    def set_password(self, password: str):
        """Set password (stores hash)."""
        self.password_hash = self._hash_password(password)

    def verify_password(self, password: str) -> bool:
        """Verify password against stored hash."""
        return self.password_hash == self._hash_password(password)

    @staticmethod
    def _hash_password(password: str) -> str:
        """Hash a password."""
        # Simple SHA256 hash - in production use bcrypt or similar
        return hashlib.sha256(password.encode('utf-8')).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        """Convert account to dictionary for serialization."""
        return {
            'account_name': self.account_name,
            'password_hash': self.password_hash,
            'is_banned': self.is_banned,
            'ban_reason': self.ban_reason,
            'is_staff': self.is_staff,
            'admin_rights': self.admin_rights,
            'kills': self.kills,
            'deaths': self.deaths,
            'online_time': self.online_time,
            'head_image': self.head_image,
            'body_image': self.body_image,
            'sword_image': self.sword_image,
            'shield_image': self.shield_image,
            'colors': self.colors,
            'max_hearts': self.max_hearts,
            'hearts': self.hearts,
            'rupees': self.rupees,
            'bombs': self.bombs,
            'arrows': self.arrows,
            'glove_power': self.glove_power,
            'sword_power': self.sword_power,
            'shield_power': self.shield_power,
            'mp': self.mp,
            'ap': self.ap,
            'level_name': self.level_name,
            'x': self.x,
            'y': self.y,
            'flags': self.flags,
            'gattribs': self.gattribs,
            'weapons': self.weapons,
            'chests_opened': self.chests_opened,
            'comments': self.comments,
            'guild_name': self.guild_name,
            'guild_nickname': self.guild_nickname,
            'profile_name': self.profile_name,
            'profile_age': self.profile_age,
            'profile_gender': self.profile_gender,
            'profile_country': self.profile_country,
            'profile_messenger': self.profile_messenger,
            'profile_email': self.profile_email,
            'profile_website': self.profile_website,
            'profile_hangout': self.profile_hangout,
            'profile_quote': self.profile_quote,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Account':
        """Create account from dictionary."""
        account = cls(account_name=data.get('account_name', ''))
        account.password_hash = data.get('password_hash', '')
        account.is_banned = data.get('is_banned', False)
        account.ban_reason = data.get('ban_reason', '')
        account.is_staff = data.get('is_staff', False)
        account.admin_rights = data.get('admin_rights', 0)
        account.kills = data.get('kills', 0)
        account.deaths = data.get('deaths', 0)
        account.online_time = data.get('online_time', 0)
        account.head_image = data.get('head_image', 'head19.png')
        account.body_image = data.get('body_image', 'body.png')
        account.sword_image = data.get('sword_image', 'sword1.png')
        account.shield_image = data.get('shield_image', 'shield1.png')
        account.colors = data.get('colors', [0, 0, 0, 0, 0])
        account.max_hearts = data.get('max_hearts', 3.0)
        account.hearts = data.get('hearts', 3.0)
        account.rupees = data.get('rupees', 0)
        account.bombs = data.get('bombs', 0)
        # arrows default must match the dataclass/GServer default of 5; a 0
        # here silently disables the whole arrow-relay path for the account.
        account.arrows = data.get('arrows', 5)
        account.glove_power = data.get('glove_power', 0)
        account.sword_power = data.get('sword_power', 1)
        account.shield_power = data.get('shield_power', 1)
        account.mp = data.get('mp', 0)
        account.ap = data.get('ap', 50)
        account.level_name = data.get('level_name', '')
        account.x = data.get('x', 30.0)
        account.y = data.get('y', 30.0)
        account.flags = data.get('flags', {})
        account.gattribs = data.get('gattribs', [''] * 30)
        account.weapons = data.get('weapons', [])
        account.chests_opened = data.get('chests_opened', [])
        account.comments = data.get('comments', '')
        account.guild_name = data.get('guild_name', '')
        account.guild_nickname = data.get('guild_nickname', '')
        account.profile_name = data.get('profile_name', '')
        account.profile_age = data.get('profile_age', '')
        account.profile_gender = data.get('profile_gender', '')
        account.profile_country = data.get('profile_country', '')
        account.profile_messenger = data.get('profile_messenger', '')
        account.profile_email = data.get('profile_email', '')
        account.profile_website = data.get('profile_website', '')
        account.profile_hangout = data.get('profile_hangout', '')
        account.profile_quote = data.get('profile_quote', '')
        return account


class AccountManager:
    """
    Manages player accounts and persistence.

    Handles:
    - Account creation and deletion
    - Password verification
    - Loading and saving accounts
    - Account listing for RC
    """

    def __init__(self, server: 'GameServer', accounts_dir: str = "accounts"):
        self.server = server
        self.accounts_dir = Path(accounts_dir)

        # In-memory account cache
        self._accounts: Dict[str, Account] = {}

        # Staff accounts (from config)
        self._staff: List[str] = []

        # Auto-save interval
        self.auto_save_interval = 300  # 5 minutes
        self._auto_save_task: Optional[asyncio.Task] = None
        self._running = False

        # Single-thread writer: serializes all account writes (a disconnect
        # save racing the auto-save sweep or an RC edit must never overlap
        # writes to the same file) and gives stop() something to drain.
        self._save_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="account-save")

        # Create accounts directory
        self.accounts_dir.mkdir(parents=True, exist_ok=True)

    async def start(self):
        """Start the account manager."""
        self._running = True
        self._auto_save_task = asyncio.create_task(self._auto_save_loop())
        logger.info(f"Account manager started (dir: {self.accounts_dir})")

    async def stop(self):
        """Stop the account manager and save all accounts."""
        self._running = False
        if self._auto_save_task:
            self._auto_save_task.cancel()
            try:
                await self._auto_save_task
            except asyncio.CancelledError:
                pass

        # Save all accounts, then drain the writer thread so the final saves
        # are actually on disk before shutdown proceeds (asyncio.run() on 3.8
        # doesn't wait for executor threads).
        await self.save_all_accounts()
        await asyncio.get_running_loop().run_in_executor(
            None, self._save_executor.shutdown)
        logger.info("Account manager stopped")

    async def _auto_save_loop(self):
        """Periodically save all accounts."""
        while self._running:
            try:
                await asyncio.sleep(self.auto_save_interval)
                await self.save_all_accounts()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Auto-save error: {e}")

    # =========================================================================
    # Account Operations
    # =========================================================================

    def get_account(self, account_name: str) -> Optional[Account]:
        """
        Get an account by name.

        Args:
            account_name: Account name

        Returns:
            Account if found, None otherwise
        """
        # Check cache
        name_lower = account_name.lower()
        if name_lower in self._accounts:
            return self._accounts[name_lower]

        # Try to load from disk
        account = self._load_account(account_name)
        if account:
            self._accounts[name_lower] = account

        return account

    def create_account(self, account_name: str, password: str = "") -> Account:
        """
        Create a new account.

        Args:
            account_name: Account name
            password: Account password

        Returns:
            The created account
        """
        name_lower = account_name.lower()

        # Check if already exists
        if name_lower in self._accounts or self._account_file_exists(account_name):
            logger.warning(f"Account already exists: {account_name}")
            return self._accounts.get(name_lower) or self._load_account(account_name)

        # Create new account
        account = Account(account_name=account_name)
        if password:
            account.set_password(password)

        # Check if staff
        if account_name in self._staff:
            account.is_staff = True

        # Save
        self._accounts[name_lower] = account
        self._save_account(account)

        logger.info(f"Created account: {account_name}")
        return account

    def delete_account(self, account_name: str) -> bool:
        """
        Delete an account.

        Args:
            account_name: Account name

        Returns:
            True if deleted
        """
        name_lower = account_name.lower()

        # Remove from cache
        self._accounts.pop(name_lower, None)

        # Delete file
        account_file = self.accounts_dir / f"{account_name}.json"
        try:
            if account_file.exists():
                account_file.unlink()
                logger.info(f"Deleted account: {account_name}")
                return True
        except Exception as e:
            logger.error(f"Error deleting account {account_name}: {e}")

        return False

    def verify_password(self, account_name: str, password: str) -> bool:
        """
        Verify a password for an account.

        Args:
            account_name: Account name
            password: Password to verify

        Returns:
            True if password is correct
        """
        account = self.get_account(account_name)
        if not account:
            return False

        return account.verify_password(password)

    def save_account(self, account: Account):
        """
        Save an account to disk.

        Args:
            account: Account to save
        """
        self._save_account(account)

    async def save_all_accounts(self):
        """Save all cached accounts to disk."""
        count = 0
        for account in self._accounts.values():
            self._save_account(account)
            count += 1
        logger.debug(f"Saved {count} accounts")

    def list_accounts(self) -> List[str]:
        """
        List all account names.

        Returns:
            List of account names
        """
        accounts = set()

        # From cache
        accounts.update(self._accounts.keys())

        # From disk
        for file in self.accounts_dir.glob("*.json"):
            accounts.add(file.stem.lower())

        return sorted(accounts)

    # =========================================================================
    # Staff Management
    # =========================================================================

    def set_staff_list(self, staff: List[str]):
        """
        Set the list of staff accounts.

        Args:
            staff: List of staff account names
        """
        self._staff = staff
        logger.info(f"Staff accounts: {', '.join(staff)}")

    def is_staff(self, account_name: str) -> bool:
        """Check if an account is staff."""
        return account_name in self._staff

    # =========================================================================
    # Player Integration
    # =========================================================================

    def load_player_from_account(self, player: 'Player', account: Account):
        """
        Load player data from account.

        Args:
            player: Player to load into
            account: Account to load from
        """
        player.head_image = account.head_image
        player.body_image = account.body_image
        player.colors = account.colors.copy()
        player.max_hearts = account.max_hearts
        # Don't let player spawn dead - restore to max hearts if saved with 0
        player.hearts = account.hearts if account.hearts > 0 else account.max_hearts
        player.rupees = account.rupees
        player.bombs = account.bombs
        player.arrows = account.arrows
        player.glove_power = account.glove_power
        player.sword_power = account.sword_power
        player.shield_power = account.shield_power
        player.mp = account.mp
        player.ap = account.ap
        player.flags = account.flags.copy()
        player.gattribs = {i: v for i, v in enumerate(account.gattribs) if v}

        # Set position if saved
        if account.level_name:
            # Don't warp here - let server handle initial warp
            pass

        logger.debug(f"Loaded player data from account {account.account_name}")

    def save_player_to_account(self, player: 'Player', account: Account):
        """
        Save player data to account.

        Args:
            player: Player to save from
            account: Account to save to
        """
        account.head_image = player.head_image
        account.body_image = player.body_image
        account.colors = player.colors.copy()
        account.max_hearts = player.max_hearts
        account.hearts = player.hearts
        account.rupees = player.rupees
        account.bombs = player.bombs
        account.arrows = player.arrows
        account.glove_power = player.glove_power
        account.sword_power = player.sword_power
        account.shield_power = player.shield_power
        account.mp = player.mp
        account.ap = player.ap
        account.flags = player.flags.copy()

        # Save gattribs
        for i, v in player.gattribs.items():
            if 0 <= i < 30:
                account.gattribs[i] = v

        # Save position
        if player.level:
            account.level_name = player.level.name
            account.x = player.x
            account.y = player.y

        self._save_account(account)
        logger.debug(f"Saved player data to account {account.account_name}")

    # =========================================================================
    # Internal Methods
    # =========================================================================

    def _load_account(self, account_name: str) -> Optional[Account]:
        """Load account from disk."""
        account_file = self.accounts_dir / f"{account_name}.json"

        if not account_file.exists():
            return None

        try:
            with open(account_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return Account.from_dict(data)
        except Exception as e:
            logger.error(f"Error loading account {account_name}: {e}")
            return None

    def _save_account(self, account: Account):
        """Save account to disk.

        Callers are a mix of sync and async methods (create_account,
        save_account, save_player_to_account, save_all_accounts), so this
        stays a sync method; the blocking write goes to the dedicated
        single-thread executor so it doesn't stall the event loop. One
        writer thread means saves for the same account can never overlap
        (a disconnect save racing the auto-save sweep used to interleave
        two json.dump()s into one corrupt file), and the temp-file +
        os.replace makes each write atomic, so a crash mid-write leaves
        the previous good file rather than a truncated one.
        """
        account_file = self.accounts_dir / f"{account.account_name}.json"
        data = account.to_dict()

        def _write():
            tmp_file = account_file.with_suffix('.json.tmp')
            try:
                with open(tmp_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_file, account_file)
            except Exception as e:
                logger.error(f"Error saving account {account.account_name}: {e}")

        try:
            self._save_executor.submit(_write)
        except RuntimeError:
            # Executor already shut down (late save during teardown) -
            # write inline rather than dropping the save.
            _write()

    def _account_file_exists(self, account_name: str) -> bool:
        """Check if account file exists."""
        account_file = self.accounts_dir / f"{account_name}.json"
        return account_file.exists()


# The 9 free-text webpage-profile fields (Player.cpp msgPLI_PROFILESET /
# ServerList.cpp msgSVI_PROFILE order). Shared by ProfileManager and the
# PLI_PROFILEGET/PROFILESET wire codec in protocol/packets.py.
PROFILE_FIELDS = ('name', 'age', 'gender', 'country', 'messenger',
                  'email', 'website', 'hangout', 'quote')


class ProfileManager:
    """
    Manages player webpage profiles (PLI_PROFILEGET/PROFILESET, PLO_PROFILE).

    On GServer-v2, these packets are opaque blobs relayed through the list
    server's SVO_GETPROF/SVO_SETPROF (ServerList.cpp msgSVI_PROFILE) to an
    external profile/webpage service - the game server itself never parses
    the 9 free-text fields, only adds the account name and online time.
    pygserver has no such external service, so the fields are persisted
    locally on the account instead (see Account.profile_* in this module).
    """

    def __init__(self, server: 'GameServer'):
        self.server = server

    def get_profile(self, account_name: str) -> Dict[str, Any]:
        """
        Get a player's profile.

        Args:
            account_name: Account name

        Returns:
            Profile dict with 'account', the 9 PROFILE_FIELDS, and
            'online_time' (formatted "H hrs M mins S secs", matching
            ServerList.cpp msgSVI_PROFILE). Empty dict if unknown account.
        """
        if not hasattr(self.server, 'account_manager'):
            return {}

        account = self.server.account_manager.get_account(account_name)
        if not account:
            return {}

        profile = {'account': account.account_name}
        for name in PROFILE_FIELDS:
            profile[name] = getattr(account, f'profile_{name}', '')
        profile['online_time'] = self._format_online_time(account.online_time)
        return profile

    @staticmethod
    def _format_online_time(seconds: int) -> str:
        """Format seconds as "H hrs M mins S secs" (ServerList.cpp msgSVI_PROFILE)."""
        seconds = int(seconds)
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours} hrs {minutes} mins {secs} secs"

    def set_profile(self, player: 'Player', profile_data: Dict[str, Any]):
        """
        Set player's own profile data.

        Args:
            player: Player updating their profile (only their own account
                is ever updated - callers must already have checked
                profile_data['account'] == player.account_name, matching
                the self-check in Player.cpp msgPLI_PROFILESET)
            profile_data: dict with any of PROFILE_FIELDS to update
        """
        if not hasattr(self.server, 'account_manager'):
            return

        account = self.server.account_manager.get_account(player.account_name)
        if not account:
            return

        for name in PROFILE_FIELDS:
            if name in profile_data:
                setattr(account, f'profile_{name}', profile_data[name])

        self.server.account_manager.save_account(account)

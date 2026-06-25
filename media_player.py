#!/usr/bin/env python3
"""
Media Server Device Client

This application connects to the Media Server API, authenticates,
downloads content, and plays it according to playlists.

Usage:
    python media_player.py --config config.json
"""

import argparse
import json
import logging
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
import threading
import queue
import subprocess
import tempfile
import hashlib
from typing import Dict, List, Optional, Union, Any

import requests
from requests.exceptions import RequestException
import netifaces
import vlc  # pip install python-vlc

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('media_player.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('media_player')

class DeviceConfig:
    """Device configuration handler"""
    
    def __init__(self, config_path: str) -> None:
        """Initialize with config file path"""
        self.config_path = config_path
        self.config = {}
        self.load_config()
        
    def load_config(self) -> None:
        """Load configuration from file"""
        try:
            with open(self.config_path, 'r') as f:
                self.config = json.load(f)
            logger.info(f"Loaded configuration from {self.config_path}")
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.warning(f"Could not load config: {e}")
            self.initialize_default_config()
            
    def save_config(self) -> None:
        """Save configuration to file"""
        with open(self.config_path, 'w') as f:
            json.dump(self.config, f, indent=4)
        logger.info(f"Saved configuration to {self.config_path}")
            
    def initialize_default_config(self) -> None:
        """Create default configuration"""
        self.config = {
            "server_url": "http://192.168.0.115:8080/api",
            "username": "device",
            "password": "device123",
            "device_name": f"MediaPlayer-{uuid.uuid4().hex[:8]}",
            "content_dir": "./content",
            "auth_token": None,
            "device_id": None,
            "user_id": None,
            "token_expires": None,
            "playlist_id": None,
            "last_sync": None,
            "loop_content": True,
            "check_interval": 60,  # seconds between checks for new content
            "display": {
                "orientation": "landscape",  # landscape or portrait
                "rotation": 0,              # 0, 90, 180, or 270 degrees
                "fullscreen": True,
                "display_number": 0,        # For multi-display setups
                "resolution": "auto"        # auto or specific like "1920x1080"
            },
            "log_level": "INFO"
        }
        self.save_config()
        
    def get(self, key: str, default: Any = None) -> Any:
        """Get a config value (supports nested configs with dot notation)"""
        if "." in key:
            main_key, sub_key = key.split(".", 1)
            if main_key in self.config and isinstance(self.config[main_key], dict):
                return self.config[main_key].get(sub_key, default)
            return default
        return self.config.get(key, default)
        
    def set(self, key: str, value: Any) -> None:
        """Set a config value and save (supports nested configs with dot notation)"""
        if "." in key:
            main_key, sub_key = key.split(".", 1)
            if main_key not in self.config:
                self.config[main_key] = {}
            if not isinstance(self.config[main_key], dict):
                self.config[main_key] = {}
            self.config[main_key][sub_key] = value
        else:
            self.config[key] = value
        self.save_config()


class MediaServerAPI:
    """API client for Media Server"""
    
    def __init__(self, config: DeviceConfig) -> None:
        """Initialize with configuration"""
        self.config = config
        self.base_url = config.get("server_url")
        self.session = requests.Session()
        self._load_auth_token()
        
    def _load_auth_token(self) -> None:
        """Load auth token from config"""
        token = self.config.get("auth_token")
        expires = self.config.get("token_expires")
        
        if token and expires:
            expires_dt = datetime.fromisoformat(expires)
            if expires_dt > datetime.now():
                self.session.headers.update({"Authorization": f"Bearer {token}"})
                logger.info("Loaded existing auth token")
            else:
                logger.info("Auth token expired, will re-authenticate")
        
    def _save_auth_token(self, token: str) -> None:
        """Save auth token to config"""
        # JWT tokens typically expire in 24 hours for this server
        expires = datetime.now() + timedelta(hours=23)
        self.config.set("auth_token", token)
        self.config.set("token_expires", expires.isoformat())
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        
    def login(self) -> bool:
        """Login to the server and get auth token"""
        url = f"{self.base_url}/auth/login"
        data = {
            "username": self.config.get("username"),
            "password": self.config.get("password")
        }

        try:
            response = self.session.post(url, json=data)
            response.raise_for_status()
            result = response.json()
            print("")
            print(response.json())
            print("")

            token = result.get("accessToken")
            self._save_auth_token(token)
            
            # Save user ID if returned
            if "user" in result and "username" in result["user"]:
                print(result)
                self.config.set("user_id", result["user"]["userId"])
                logger.info(f"Logged in as user: {result['user']['username']}")
            
            return True
        except RequestException as e:
            logger.error(f"Login failed: {e}")
            return False
    
    def register_device(self) -> bool:
        """Register this device with the server"""
        if not self.config.get("user_id"):
            logger.error("Cannot register device: User ID not found")
            return False
            
        # Get MAC address of the primary interface
        mac_address = self._get_mac_address()
        if not mac_address:
            logger.error("Failed to get MAC address")
            return False
            
        url = f"{self.base_url}/devices/user/{self.config.get('user_id')}"
        data = {
            "macAddress": mac_address,
            "deviceName": self.config.get("device_name"),
            "enabled": True
        }
        
        try:
            response = self.session.post(url, json=data)
            
            # If device already exists, try to get it
            if response.status_code == 400 and "MAC address already registered" in response.text:
                logger.info("Device already registered, retrieving device info")
                return self.get_device_by_mac(mac_address)
                
            response.raise_for_status()
            device = response.json()
            self.config.set("device_id", device.get("id"))
            logger.info(f"Registered device: {device.get('deviceName')} ({device.get('id')})")
            return True
        except RequestException as e:
            logger.error(f"Device registration failed: {e}")
            return False
    
    def get_device_by_mac(self, mac_address: str) -> bool:
        """Get device info by MAC address"""
        url = f"{self.base_url}/devices/mac/{mac_address}"
        
        try:
            response = self.session.get(url)
            response.raise_for_status()
            device = response.json()
            self.config.set("device_id", device.get("id"))
            logger.info(f"Retrieved device: {device.get('deviceName')} ({device.get('id')})")
            return True
        except RequestException as e:
            logger.error(f"Failed to get device by MAC: {e}")
            return False
    
    def get_playlists(self) -> List[Dict]:
        """Get available playlists for user"""
        if not self.config.get("user_id"):
            logger.error("Cannot get playlists: User ID not found")
            return []
            
        url = f"{self.base_url}/playlists/user/{self.config.get('user_id')}"
        
        try:
            response = self.session.get(url)
            response.raise_for_status()
            playlists = response.json()
            logger.info(f"Retrieved {len(playlists)} playlists")
            return playlists
        except RequestException as e:
            logger.error(f"Failed to get playlists: {e}")
            return []
    
    def get_playlist_contents(self, playlist_id: str) -> List[Dict]:
        """Get contents of a playlist"""
        url = f"{self.base_url}/playlists/{playlist_id}/contents"
        
        try:
            response = self.session.get(url)
            response.raise_for_status()
            contents = response.json()
            logger.info(f"Retrieved {len(contents)} items from playlist {playlist_id}")
            return contents
        except RequestException as e:
            logger.error(f"Failed to get playlist contents: {e}")
            return []
    
    def get_content_details(self, content_id: str) -> Optional[Dict]:
        """Get details of a content item"""
        url = f"{self.base_url}/content/{content_id}"
        
        try:
            response = self.session.get(url)
            response.raise_for_status()
            content = response.json()
            return content
        except RequestException as e:
            logger.error(f"Failed to get content details: {e}")
            return None
    
    def download_content(self, content_id: str, destination_path: str) -> bool:
        """Download content file"""
        url = f"{self.base_url}/content/{content_id}/stream"
        
        try:
            response = self.session.get(url, stream=True)
            response.raise_for_status()
            
            # Make sure parent directory exists
            os.makedirs(os.path.dirname(destination_path), exist_ok=True)
            
            # Save file
            with open(destination_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            logger.info(f"Downloaded content to {destination_path}")
            return True
        except RequestException as e:
            logger.error(f"Failed to download content: {e}")
            return False
    
    def _get_mac_address(self) -> Optional[str]:
        """Get MAC address of primary interface"""
        try:
            # Find the primary interface (the one used for default route)
            gateways = netifaces.gateways()
            default_interface = gateways['default'][netifaces.AF_INET][1]
            
            # Get MAC address for this interface
            mac = netifaces.ifaddresses(default_interface)[netifaces.AF_LINK][0]['addr']
            return mac
        except (KeyError, IndexError, ValueError) as e:
            logger.error(f"Error getting MAC address: {e}")
            return None


class ContentManager:
    """Manages content files, downloads, and caching"""
    
    def __init__(self, config: DeviceConfig, api: MediaServerAPI) -> None:
        """Initialize with configuration and API client"""
        self.config = config
        self.api = api
        self.content_dir = Path(config.get("content_dir"))
        self.content_dir.mkdir(exist_ok=True)
        self.content_cache = {}  # Maps content_id to local file path
        self.load_content_cache()
    
    def load_content_cache(self) -> None:
        """Load content cache info from disk"""
        cache_file = self.content_dir / "content_cache.json"
        if cache_file.exists():
            try:
                with open(cache_file, 'r') as f:
                    self.content_cache = json.load(f)
                logger.info(f"Loaded {len(self.content_cache)} items from content cache")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load content cache: {e}")
                self.content_cache = {}
    
    def save_content_cache(self) -> None:
        """Save content cache info to disk"""
        cache_file = self.content_dir / "content_cache.json"
        try:
            with open(cache_file, 'w') as f:
                json.dump(self.content_cache, f, indent=4)
        except IOError as e:
            logger.error(f"Failed to save content cache: {e}")
    
    def get_content_path(self, content_id: str) -> Optional[str]:
        """Get local path for a content item"""
        if content_id in self.content_cache:
            path = self.content_cache[content_id]["path"]
            if os.path.exists(path):
                return path
        return None
    
    def download_playlist_content(self, playlist_id: str) -> List[Dict]:
        """Download all content for a playlist"""
        # Get playlist contents
        playlist_contents = self.api.get_playlist_contents(playlist_id)
        downloaded_content = []
        
        for item in playlist_contents:
            content_id = item.get("contentId")
            if not content_id:
                continue
                
            # Get content details
            content_details = self.api.get_content_details(content_id)
            if not content_details:
                continue
                
            # Check if we already have this content
            content_path = self.get_content_path(content_id)
            if not content_path:
                # Download the content
                title = content_details.get("title", "unknown")
                file_type = content_details.get("fileType", "")
                ext = file_type.split("/")[-1] if "/" in file_type else "mp4"
                
                # Create a safe filename from the title
                safe_title = "".join(c if c.isalnum() else "_" for c in title)
                filename = f"{content_id}_{safe_title}.{ext}"
                content_path = str(self.content_dir / filename)
                
                success = self.api.download_content(content_id, content_path)
                if not success:
                    continue
                
                # Add to cache
                self.content_cache[content_id] = {
                    "path": content_path,
                    "title": title,
                    "type": file_type,
                    "download_time": datetime.now().isoformat()
                }
                self.save_content_cache()
            
            # Add to downloaded content list with display order
            downloaded_content.append({
                "content_id": content_id,
                "path": content_path,
                "display_order": item.get("displayOrder", 0),
                "title": self.content_cache[content_id]["title"]
            })
        
        # Sort by display order
        downloaded_content.sort(key=lambda x: x["display_order"])
        return downloaded_content
    
    def cleanup_unused_content(self, active_content_ids: List[str]) -> None:
        """Remove content that's not in any active playlist"""
        # Find unused content
        unused_content = []
        for content_id, content_info in self.content_cache.items():
            if content_id not in active_content_ids:
                unused_content.append(content_id)
                if os.path.exists(content_info["path"]):
                    try:
                        os.remove(content_info["path"])
                        logger.info(f"Removed unused content: {content_info['path']}")
                    except OSError as e:
                        logger.error(f"Failed to remove content file: {e}")
        
        # Remove from cache
        for content_id in unused_content:
            del self.content_cache[content_id]
        
        if unused_content:
            self.save_content_cache()
            logger.info(f"Removed {len(unused_content)} unused content items from cache")


class MediaPlayer:
    """Content playback manager"""
    
    def __init__(self, content_manager: ContentManager) -> None:
        """Initialize with content manager"""
        self.content_manager = content_manager
        self.config = content_manager.config
        self.api = content_manager.api
        self.playlist = []
        self.current_index = 0
        self.running = False
        self.player = None
        self.play_thread = None
        self.command_queue = queue.Queue()
        
    def load_playlist(self, playlist_id: str) -> bool:
        """Load content for a playlist"""
        self.playlist = self.content_manager.download_playlist_content(playlist_id)
        print("####")
        print(self.playlist)
        print("####")

        if not self.playlist:
            logger.error(f"No content found in playlist {playlist_id}")
            return False
        
        logger.info(f"Loaded {len(self.playlist)} items from playlist {playlist_id}")
        return True
    
    def start_playback(self) -> None:
        """Start playing content"""
        if self.running:
            logger.warning("Playback already running")
            return
            
        if not self.playlist:
            logger.error("No playlist loaded")
            return
            
        self.running = True
        self.play_thread = threading.Thread(target=self._playback_loop)
        self.play_thread.daemon = True
        self.play_thread.start()
        logger.info("Started playback thread")
    
    def stop_playback(self) -> None:
        """Stop playing content"""
        if not self.running:
            return
            
        self.running = False
        self.command_queue.put("stop")
        if self.play_thread:
            self.play_thread.join(timeout=2)
        logger.info("Stopped playback")
    
    def next_content(self) -> None:
        """Skip to next content item"""
        self.command_queue.put("next")
    
    def previous_content(self) -> None:
        """Go back to previous content item"""
        self.command_queue.put("previous")
    
    def _playback_loop(self) -> None:
        """Main playback loop"""
        self.current_index = 0
        
        while self.running:
            if not self.playlist:
                time.sleep(1)
                continue
                
            # Get current content
            if self.current_index >= len(self.playlist):
                if self.config.get("loop_content", True):
                    self.current_index = 0
                else:
                    # End of playlist and not looping
                    self.running = False
                    break
            
            content = self.playlist[self.current_index]
            path = content["path"]
            
            if not os.path.exists(path):
                logger.error(f"Content file not found: {path}")
                self.current_index += 1
                continue
            
            # Get display settings
            orientation = self.config.get("display.orientation", "landscape")
            rotation = self.config.get("display.rotation", 0)
            fullscreen = self.config.get("display.fullscreen", True)
            
            # Create VLC instance and media player
            try:
                # Setup VLC with appropriate parameters
                vlc_args = []
                
                # Orientation and rotation parameters for VLC
                if orientation == "portrait" or rotation in [90, 270]:
                    # For portrait mode or 90/270 degree rotation, we need transform filter
                    # Note: VLC transform filter rotates the content inside the window,
                    # not the actual window orientation
                    transform_value = None
                    if rotation == 90:
                        transform_value = "90"
                    elif rotation == 180:
                        transform_value = "180"
                    elif rotation == 270:
                        transform_value = "270"
                    
                    if transform_value:
                        vlc_args.append(f"--video-filter=transform{{{transform_value}}}")
                
                # Apply any other VLC arguments as needed
                if fullscreen:
                    vlc_args.append("--fullscreen")
                
                # Create VLC instance with arguments
                instance = vlc.Instance(vlc_args)
                self.player = instance.media_player_new()
                media = instance.media_new(path)
                self.player.set_media(media)
                
                # Set fullscreen mode
                if fullscreen:
                    self.player.set_fullscreen(True)
                
                # Start playback
                self.player.play()
                
                logger.info(f"Playing content: {content['title']} ({content['content_id']}) with orientation={orientation}, rotation={rotation}")
                
                # Wait for player to start
                time.sleep(1)
                
                # Wait for playback to finish or command
                while self.running and self.player.get_state() != vlc.State.Ended:
                    try:
                        cmd = self.command_queue.get(timeout=0.5)
                        if cmd == "stop":
                            self.player.stop()
                            break
                        elif cmd == "next":
                            self.player.stop()
                            self.current_index += 1
                            break
                        elif cmd == "previous":
                            self.player.stop()
                            self.current_index = max(0, self.current_index - 1)
                            break
                        elif cmd == "toggle_fullscreen":
                            is_fullscreen = self.player.get_fullscreen()
                            self.player.set_fullscreen(not is_fullscreen)
                    except queue.Empty:
                        # Check if playback has ended
                        if self.player.get_state() in [vlc.State.Ended, vlc.State.Error, vlc.State.Stopped]:
                            break
                
                # If we got here naturally (not from a command), move to next item
                if self.running and self.player.get_state() == vlc.State.Ended:
                    self.current_index += 1
                
                # Clean up
                self.player.release()
                self.player = None
                
            except Exception as e:
                logger.error(f"Playback error: {e}")
                self.current_index += 1
                time.sleep(1)


class DisplayManager:
    """Handles display settings and orientation"""
    
    def __init__(self, config: DeviceConfig) -> None:
        """Initialize with configuration"""
        self.config = config
        
    def setup_display(self) -> None:
        """Configure display settings based on config"""
        orientation = self.config.get("display.orientation", "landscape")
        rotation = self.config.get("display.rotation", 0)
        
        logger.info(f"Setting up display: orientation={orientation}, rotation={rotation}")
        
        # Check what platform we're on
        if sys.platform == 'linux':
            self._setup_linux_display(orientation, rotation)
        elif sys.platform == 'darwin':  # macOS
            logger.info("Display rotation on macOS requires system settings changes")
        elif sys.platform == 'win32':
            logger.info("Display rotation on Windows requires system settings changes")
        
    def _setup_linux_display(self, orientation: str, rotation: int) -> None:
        """Set up display on Linux"""
        try:
            # For Raspberry Pi specifically
            is_raspberry_pi = os.path.exists('/proc/device-tree/model') and 'raspberry pi' in open('/proc/device-tree/model').read().lower()
            
            if is_raspberry_pi:
                self._setup_raspberry_pi_display(orientation, rotation)
            else:
                # For other Linux systems with X11
                self._setup_x11_display(orientation, rotation)
        except Exception as e:
            logger.error(f"Failed to configure display: {e}")
    
    def _setup_raspberry_pi_display(self, orientation: str, rotation: int) -> None:
        """Configure display on Raspberry Pi"""
        # Map orientation to rotation if rotation is not explicitly set
        if orientation == "portrait" and rotation == 0:
            rotation = 90
            
        # Check if we need to modify config.txt
        config_file = '/boot/config.txt'
        if os.path.exists(config_file):
            try:
                # Read current config
                with open(config_file, 'r') as f:
                    lines = f.readlines()
                
                # Check for existing display_rotate setting
                rotate_set = False
                for i, line in enumerate(lines):
                    if line.strip().startswith('display_rotate='):
                        lines[i] = f'display_rotate={rotation // 90}\n'
                        rotate_set = True
                        break
                
                # Add setting if not found
                if not rotate_set:
                    lines.append(f'display_rotate={rotation // 90}\n')
                
                # Try to write (may require sudo)
                try:
                    with open(config_file, 'w') as f:
                        f.writelines(lines)
                    logger.info(f"Updated {config_file} with display_rotate={rotation // 90}")
                    logger.warning("You may need to reboot for display rotation to take effect")
                except PermissionError:
                    logger.warning(f"Cannot write to {config_file}, trying with sudo...")
                    temp_file = '/tmp/config.txt.tmp'
                    with open(temp_file, 'w') as f:
                        f.writelines(lines)
                    subprocess.run(['sudo', 'cp', temp_file, config_file], check=False)
                    os.remove(temp_file)
                    logger.warning("You may need to reboot for display rotation to take effect")
            except Exception as e:
                logger.error(f"Error modifying config.txt: {e}")
        
        # For immediate effect, try using tvservice (may not work for all rotation angles)
        try:
            if shutil.which('tvservice'):
                # First get current mode
                output = subprocess.check_output(['tvservice', '-s']).decode()
                if 'HDMI' in output:
                    if rotation == 0:
                        subprocess.run(['tvservice', '-p'], check=False)
                    else:
                        logger.info("For immediate rotation effect, a reboot is needed")
        except Exception as e:
            logger.error(f"Error using tvservice: {e}")
    
    def _setup_x11_display(self, orientation: str, rotation: int) -> None:
        """Configure display rotation on X11 systems"""
        try:
            if shutil.which('xrandr'):
                # Get the list of displays
                output = subprocess.check_output(['xrandr', '--listmonitors']).decode()
                displays = []
                
                for line in output.splitlines()[1:]:  # Skip the first line
                    parts = line.split()
                    if len(parts) >= 2:
                        displays.append(parts[-1])
                
                display_number = self.config.get("display.display_number", 0)
                if display_number < len(displays):
                    display_name = displays[display_number]
                    
                    # Map rotation
                    rotation_str = "normal"
                    if rotation == 90:
                        rotation_str = "right"
                    elif rotation == 180:
                        rotation_str = "inverted"
                    elif rotation == 270:
                        rotation_str = "left"
                    
                    # Apply rotation
                    subprocess.run(['xrandr', '--output', display_name, '--rotate', rotation_str], check=False)
                    logger.info(f"Applied rotation {rotation_str} to display {display_name}")
                else:
                    logger.warning(f"Display number {display_number} not found. Available displays: {displays}")
            else:
                logger.warning("xrandr not found, cannot set display rotation")
        except Exception as e:
            logger.error(f"Error configuring X11 display: {e}")

class DeviceApplication:
    """Main device application"""
    
    def __init__(self, config_path: str) -> None:
        """Initialize with config file path"""
        self.config = DeviceConfig(config_path)
        self.display_manager = DisplayManager(self.config)
        self.api = MediaServerAPI(self.config)
        self.content_manager = ContentManager(self.config, self.api)
        self.player = MediaPlayer(self.content_manager)
        self.running = False
        self.sync_thread = None
        
        # Set log level from config
        log_level = self.config.get("log_level", "INFO")
        logger.setLevel(logging.getLevelName(log_level))
    
    def start(self) -> None:
        """Start the application"""
        logger.info("Starting media player application")
        self.running = True
        
        # Configure display settings
        self.display_manager.setup_display()
        
        # Authenticate
        if not self._ensure_authenticated():
            logger.error("Authentication failed, exiting")
            return
            
        # Initialize playlists
        self._initialize_playlists()
        
        # Start background sync thread
        self.sync_thread = threading.Thread(target=self._sync_loop)
        self.sync_thread.daemon = True
        self.sync_thread.start()
        
        # Start playback
        active_playlist = self.config.get("playlist_id")
        if active_playlist:
            if self.player.load_playlist(active_playlist):
                self.player.start_playback()
                
        # Wait for signal to quit
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Received interrupt, shutting down")
            self.stop()
    
    def stop(self) -> None:
        """Stop the application"""
        logger.info("Stopping media player application")
        self.running = False
        if self.player:
            self.player.stop_playback()
        logger.info("Application stopped")
    
    def _ensure_authenticated(self) -> bool:
        """Make sure we're authenticated, register device if needed"""
        # Check if we need to log in
        token = self.config.get("auth_token")
        if not token or not self.config.get("user_id"):
            logger.info("No auth token, logging in")
            if not self.api.login():
                return False
        
        # Check if device is registered
        if not self.config.get("device_id"):
            logger.info("Device not registered, registering")
            if not self.api.register_device():
                return False
        
        return True
    
    def _initialize_playlists(self) -> None:
        """Initialize playlists"""
        # Get available playlists
        playlists = self.api.get_playlists()
        if not playlists:
            logger.warning("No playlists available")
            return
            
        # If no playlist is selected, use the first one
        if not self.config.get("playlist_id") and playlists:
            self.config.set("playlist_id", playlists[0]["id"])
            logger.info(f"Selected playlist: {playlists[0]['name']} ({playlists[0]['id']})")
    
    def _sync_loop(self) -> None:
        """Background sync loop to check for new content"""
        check_interval = self.config.get("check_interval", 60)
        
        while self.running:
            try:
                # Wait for check interval
                for _ in range(check_interval):
                    if not self.running:
                        break
                    time.sleep(1)
                
                if not self.running:
                    break
                
                # Ensure we're authenticated
                if not self._ensure_authenticated():
                    logger.error("Re-authentication failed")
                    continue
                
                # Check for playlist changes
                self._check_for_updates()
                
            except Exception as e:
                logger.error(f"Error in sync loop: {e}")
                time.sleep(10)  # Wait a bit before retrying
    
    def _check_for_updates(self) -> None:
        """Check for updates to playlists and content"""
        # Get available playlists
        playlists = self.api.get_playlists()
        if not playlists:
            return
            
        # Check if our playlist still exists and is the most recent
        current_playlist_id = self.config.get("playlist_id")
        current_exists = any(p["id"] == current_playlist_id for p in playlists)
        
        if not current_exists and playlists:
            # Our playlist is gone, switch to the first available
            logger.info(f"Current playlist {current_playlist_id} no longer exists, switching to {playlists[0]['id']}")
            self.config.set("playlist_id", playlists[0]["id"])
            
            # Reload and restart playback
            self.player.stop_playback()
            if self.player.load_playlist(playlists[0]["id"]):
                self.player.start_playback()
        elif current_exists:
            # Check if our playlist has new content
            for playlist in playlists:
                if playlist["id"] == current_playlist_id:
                    # Check if the playlist was modified since our last sync
                    last_sync = self.config.get("last_sync")
                    last_modified = playlist.get("lastModifiedDate")
                    
                    if not last_sync or (last_modified and last_modified > last_sync):
                        logger.info("Playlist has been updated, reloading content")
                        
                        # Reload and restart playback
                        self.player.stop_playback()
                        if self.player.load_playlist(current_playlist_id):
                            self.player.start_playback()
                        
                        # Update last sync time
                        self.config.set("last_sync", datetime.now().isoformat())
                    break


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Media Server Device Client")
    parser.add_argument("--config", default="config.json", help="Path to config file")
    args = parser.parse_args()
    
    app = DeviceApplication(args.config)
    app.start()


if __name__ == "__main__":
    main()

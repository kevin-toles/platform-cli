#!/usr/bin/env python3
"""
Platform CLI - Unified service orchestration for local development.

Usage:
    platform up --mode=hybrid       # Start all services
    platform down                   # Stop all services  
    platform status                 # Show service status
    platform doctor                 # Diagnose issues
    platform logs [service]         # View logs

Implements: ARCHITECTURE_DECISION_RECORD.md
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

# =============================================================================
# Configuration
# =============================================================================

PLATFORM_ROOT = Path("/Users/kevintoles/POC")
TOPOLOGY_FILE = PLATFORM_ROOT / "platform-cli" / "topology.yaml"
PID_DIR = PLATFORM_ROOT / "platform-cli" / ".pids"
LOG_DIR = PLATFORM_ROOT / "platform-cli" / "logs"

# Colors
GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[1;33m"
BLUE = "\033[0;34m"
NC = "\033[0m"


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class ServiceConfig:
    """Configuration for a single service."""
    name: str
    path: Path
    port: int
    health_endpoint: str
    start_command: dict  # mode -> command
    depends_on: list[str]
    env: dict[str, str]


# =============================================================================
# Service Manager
# =============================================================================

class PlatformManager:
    """Manages platform services across different modes."""

    def __init__(self, mode: str):
        self.mode = mode
        self.topology = self._load_topology()
        self.services = self._parse_services()
        
        # Ensure directories exist
        PID_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    def _load_topology(self) -> dict:
        """Load topology.yaml configuration."""
        if not TOPOLOGY_FILE.exists():
            print(f"{RED}Error: topology.yaml not found at {TOPOLOGY_FILE}{NC}")
            print(f"Run: platform init")
            sys.exit(1)
        
        with open(TOPOLOGY_FILE) as f:
            return yaml.safe_load(f)

    def _parse_services(self) -> dict[str, ServiceConfig]:
        """Parse services from topology."""
        services = {}
        for name, config in self.topology.get("services", {}).items():
            services[name] = ServiceConfig(
                name=name,
                path=PLATFORM_ROOT / config["path"],
                port=config["port"],
                health_endpoint=config.get("health_endpoint", "/health"),
                start_command=config.get("start", {}),
                depends_on=config.get("depends_on", []),
                env=config.get("env", {}),
            )
        return services

    def _get_startup_order(self) -> list[str]:
        """Get services in dependency order."""
        # Simple topological sort
        visited = set()
        order = []

        def visit(name: str):
            if name in visited:
                return
            visited.add(name)
            service = self.services.get(name)
            if service:
                for dep in service.depends_on:
                    visit(dep)
                order.append(name)

        for name in self.services:
            visit(name)
        return order

    def _check_port(self, port: int) -> Optional[int]:
        """Check if port is in use, return PID if so."""
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.strip().split()[0])
        except Exception:
            pass
        return None

    def _wait_for_health(self, service: ServiceConfig, timeout: int = 60) -> bool:
        """Wait for service to become healthy."""
        import urllib.request
        import urllib.error

        url = f"http://localhost:{service.port}{service.health_endpoint}"
        start = time.time()
        
        while time.time() - start < timeout:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status == 200:
                        return True
            except (urllib.error.URLError, ConnectionRefusedError, TimeoutError):
                pass
            time.sleep(1)
        
        return False

    def _save_pid(self, service_name: str, pid: int):
        """Save service PID to file."""
        pid_file = PID_DIR / f"{service_name}.pid"
        pid_file.write_text(str(pid))

    def _get_pid(self, service_name: str) -> Optional[int]:
        """Get saved PID for service."""
        pid_file = PID_DIR / f"{service_name}.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                # Check if process still exists
                os.kill(pid, 0)
                return pid
            except (ValueError, ProcessLookupError, PermissionError):
                pid_file.unlink(missing_ok=True)
        return None

    def _remove_pid(self, service_name: str):
        """Remove PID file."""
        pid_file = PID_DIR / f"{service_name}.pid"
        pid_file.unlink(missing_ok=True)

    def _start_docker_service(self, service: ServiceConfig) -> bool:
        """Start a Docker-based service."""
        print(f"  {BLUE}Starting {service.name} (docker)...{NC}")
        
        # Check if already running
        existing_pid = self._check_port(service.port)
        if existing_pid:
            print(f"    {YELLOW}Port {service.port} already in use (PID {existing_pid}){NC}")
            return True
        
        log_file = LOG_DIR / f"{service.name}.log"
        
        try:
            # Run docker compose up -d
            result = subprocess.run(
                ["docker", "compose", "up", "-d"],
                cwd=service.path,
                capture_output=True,
                text=True,
            )
            
            if result.returncode != 0:
                print(f"    {RED}Failed: {result.stderr}{NC}")
                return False
            
            # Wait for health
            if self._wait_for_health(service, timeout=30):
                print(f"    {GREEN}✓ {service.name} healthy on port {service.port}{NC}")
                return True
            else:
                print(f"    {RED}✗ {service.name} failed health check{NC}")
                return False
                
        except Exception as e:
            print(f"    {RED}Error: {e}{NC}")
            return False

    def _start_native_service(self, service: ServiceConfig) -> bool:
        """Start a native (non-Docker) service."""
        print(f"  {BLUE}Starting {service.name} (native)...{NC}")
        
        # Check if already running
        existing_pid = self._check_port(service.port)
        if existing_pid:
            print(f"    {YELLOW}Port {service.port} already in use (PID {existing_pid}){NC}")
            self._save_pid(service.name, existing_pid)
            return True
        
        log_file = LOG_DIR / f"{service.name}.log"
        
        # Get command for this mode
        cmd = service.start_command.get(self.mode) or service.start_command.get("native")
        if not cmd:
            print(f"    {RED}No start command for mode '{self.mode}'{NC}")
            return False
        
        # Build environment
        env = os.environ.copy()
        env.update(service.env)
        
        try:
            # Start process with nohup, redirect output to log
            with open(log_file, "w") as log:
                process = subprocess.Popen(
                    cmd,
                    shell=True,
                    cwd=service.path,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,  # Detach from terminal
                )
            
            self._save_pid(service.name, process.pid)
            
            # Wait for health
            if self._wait_for_health(service, timeout=60):
                print(f"    {GREEN}✓ {service.name} healthy on port {service.port}{NC}")
                return True
            else:
                print(f"    {RED}✗ {service.name} failed health check{NC}")
                print(f"    Check logs: {log_file}")
                return False
                
        except Exception as e:
            print(f"    {RED}Error: {e}{NC}")
            return False

    def _stop_docker_service(self, service: ServiceConfig):
        """Stop a Docker-based service."""
        print(f"  Stopping {service.name} (docker)...")
        try:
            subprocess.run(
                ["docker", "compose", "down"],
                cwd=service.path,
                capture_output=True,
            )
            print(f"    {GREEN}✓ Stopped{NC}")
        except Exception as e:
            print(f"    {RED}Error: {e}{NC}")

    def _stop_native_service(self, service: ServiceConfig):
        """Stop a native service."""
        print(f"  Stopping {service.name} (native)...")
        
        # Try saved PID first
        pid = self._get_pid(service.name)
        if not pid:
            # Try port lookup
            pid = self._check_port(service.port)
        
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(1)
                # Force kill if still running
                try:
                    os.kill(pid, 0)
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                print(f"    {GREEN}✓ Stopped (PID {pid}){NC}")
            except ProcessLookupError:
                print(f"    {YELLOW}Process already stopped{NC}")
            except Exception as e:
                print(f"    {RED}Error: {e}{NC}")
        else:
            print(f"    {YELLOW}Not running{NC}")
        
        self._remove_pid(service.name)

    # =========================================================================
    # Public Commands
    # =========================================================================

    def up(self):
        """Start all services in dependency order."""
        print(f"\n{GREEN}Starting platform in {self.mode} mode...{NC}\n")
        
        # Preflight checks
        if not self._preflight_checks():
            sys.exit(1)
        
        order = self._get_startup_order()
        mode_config = self.topology.get("modes", {}).get(self.mode, {})
        
        failed = []
        for name in order:
            service = self.services[name]
            service_mode = mode_config.get(name, "docker")
            
            if service_mode == "docker":
                success = self._start_docker_service(service)
            else:
                success = self._start_native_service(service)
            
            if not success:
                failed.append(name)
        
        print()
        if failed:
            print(f"{RED}Failed to start: {', '.join(failed)}{NC}")
            sys.exit(1)
        else:
            print(f"{GREEN}✓ Platform ready!{NC}")

    def down(self):
        """Stop all services."""
        print(f"\n{YELLOW}Stopping platform...{NC}\n")
        
        # Stop in reverse dependency order
        order = list(reversed(self._get_startup_order()))
        mode_config = self.topology.get("modes", {}).get(self.mode, {})
        
        for name in order:
            service = self.services[name]
            service_mode = mode_config.get(name, "docker")
            
            if service_mode == "docker":
                self._stop_docker_service(service)
            else:
                self._stop_native_service(service)
        
        print(f"\n{GREEN}✓ Platform stopped{NC}")

    def status(self):
        """Show status of all services."""
        print(f"\n{BLUE}Platform Status (mode: {self.mode}){NC}\n")
        
        mode_config = self.topology.get("modes", {}).get(self.mode, {})
        
        for name in self._get_startup_order():
            service = self.services[name]
            service_mode = mode_config.get(name, "docker")
            
            # Check health
            import urllib.request
            import urllib.error
            
            url = f"http://localhost:{service.port}{service.health_endpoint}"
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status == 200:
                        status = f"{GREEN}● healthy{NC}"
                    else:
                        status = f"{YELLOW}● unhealthy{NC}"
            except Exception:
                status = f"{RED}● stopped{NC}"
            
            pid = self._get_pid(name) or self._check_port(service.port) or "-"
            print(f"  {name:25} {status:20} port:{service.port:5} pid:{pid} ({service_mode})")
        
        print()

    def doctor(self):
        """Run diagnostic checks."""
        print(f"\n{BLUE}Platform Doctor{NC}\n")
        
        issues = []
        
        # Check Docker
        print("Checking Docker...")
        try:
            result = subprocess.run(["docker", "info"], capture_output=True)
            if result.returncode == 0:
                print(f"  {GREEN}✓ Docker running{NC}")
            else:
                print(f"  {RED}✗ Docker not running{NC}")
                issues.append("Start Docker Desktop")
        except FileNotFoundError:
            print(f"  {RED}✗ Docker not installed{NC}")
            issues.append("Install Docker")
        
        # Check ports
        print("\nChecking ports...")
        for name, service in self.services.items():
            pid = self._check_port(service.port)
            if pid:
                print(f"  Port {service.port} ({name}): {YELLOW}in use by PID {pid}{NC}")
            else:
                print(f"  Port {service.port} ({name}): {GREEN}available{NC}")
        
        # Check model files
        print("\nChecking model files...")
        models_dir = Path("/Users/kevintoles/POC/ai-models/models")
        if models_dir.exists():
            gguf_files = list(models_dir.glob("*.gguf"))
            print(f"  {GREEN}✓ Found {len(gguf_files)} model files{NC}")
        else:
            print(f"  {RED}✗ Models directory not found: {models_dir}{NC}")
            issues.append(f"Create {models_dir} and add GGUF files")
        
        # Check topology
        print("\nChecking topology...")
        if TOPOLOGY_FILE.exists():
            print(f"  {GREEN}✓ topology.yaml found{NC}")
        else:
            print(f"  {RED}✗ topology.yaml not found{NC}")
            issues.append("Run: platform init")
        
        # Summary
        print()
        if issues:
            print(f"{YELLOW}Issues found:{NC}")
            for issue in issues:
                print(f"  • {issue}")
        else:
            print(f"{GREEN}✓ All checks passed{NC}")

    def logs(self, service_name: Optional[str] = None):
        """View service logs."""
        if service_name:
            log_file = LOG_DIR / f"{service_name}.log"
            if log_file.exists():
                subprocess.run(["tail", "-f", str(log_file)])
            else:
                print(f"{RED}No logs for {service_name}{NC}")
        else:
            # Show all log files
            print(f"\n{BLUE}Available logs:{NC}")
            for log_file in LOG_DIR.glob("*.log"):
                print(f"  {log_file.stem}")

    def _preflight_checks(self) -> bool:
        """Run preflight checks before starting."""
        print(f"{BLUE}Running preflight checks...{NC}\n")
        
        passed = True
        
        # Check required ports are free (or have our services)
        for name, service in self.services.items():
            pid = self._check_port(service.port)
            if pid:
                saved_pid = self._get_pid(name)
                if pid != saved_pid:
                    print(f"  {RED}✗ Port {service.port} in use by unknown process (PID {pid}){NC}")
                    print(f"    Run: kill {pid}")
                    passed = False
        
        if passed:
            print(f"  {GREEN}✓ All preflight checks passed{NC}\n")
        
        return passed


# =============================================================================
# CLI Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Platform CLI - Unified service orchestration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  platform up --mode=hybrid    Start all services in hybrid mode
  platform down                Stop all services
  platform status              Show service status
  platform doctor              Run diagnostics
  platform logs inference      View inference service logs
        """,
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # up command
    up_parser = subparsers.add_parser("up", help="Start platform")
    up_parser.add_argument(
        "--mode", "-m",
        choices=["docker", "hybrid", "native"],
        default="hybrid",
        help="Deployment mode (default: hybrid)",
    )
    
    # down command
    subparsers.add_parser("down", help="Stop platform")
    
    # status command
    subparsers.add_parser("status", help="Show status")
    
    # doctor command
    subparsers.add_parser("doctor", help="Run diagnostics")
    
    # logs command
    logs_parser = subparsers.add_parser("logs", help="View logs")
    logs_parser.add_argument("service", nargs="?", help="Service name")
    
    # init command
    subparsers.add_parser("init", help="Initialize topology.yaml")
    
    args = parser.parse_args()
    
    if args.command == "init":
        init_topology()
        return
    
    if not args.command:
        parser.print_help()
        return
    
    # Get mode from args or environment
    mode = getattr(args, "mode", None) or os.environ.get("PLATFORM_MODE", "hybrid")
    
    manager = PlatformManager(mode=mode)
    
    if args.command == "up":
        manager.up()
    elif args.command == "down":
        manager.down()
    elif args.command == "status":
        manager.status()
    elif args.command == "doctor":
        manager.doctor()
    elif args.command == "logs":
        manager.logs(args.service)


def init_topology():
    """Create default topology.yaml."""
    topology = {
        "version": "1.0",
        "modes": {
            "docker": {
                "qdrant": "docker",
                "llm-gateway": "docker",
                "semantic-search": "docker",
                "code-orchestrator": "docker",
                "inference": "docker",
            },
            "hybrid": {
                "qdrant": "docker",
                "llm-gateway": "docker",
                "semantic-search": "docker",
                "code-orchestrator": "docker",
                "inference": "native",  # Native for Metal GPU
            },
            "native": {
                "qdrant": "docker",  # Qdrant always Docker
                "llm-gateway": "native",
                "semantic-search": "native",
                "code-orchestrator": "native",
                "inference": "native",
            },
        },
        "services": {
            "qdrant": {
                "path": "qdrant",
                "port": 6333,
                "health_endpoint": "/healthz",
                "start": {
                    "docker": "docker compose up -d",
                },
                "depends_on": [],
            },
            "inference": {
                "path": "inference-service",
                "port": 8085,
                "health_endpoint": "/health",
                "start": {
                    "docker": "docker compose up -d",
                    "native": "source .venv/bin/activate && python -m uvicorn src.main:app --host 0.0.0.0 --port 8085",
                },
                "env": {
                    "INFERENCE_MODELS_DIR": "/Users/kevintoles/POC/ai-models/models",
                    "INFERENCE_CONFIG_DIR": "/Users/kevintoles/POC/inference-service/config",
                    "INFERENCE_GPU_LAYERS": "-1",
                    "INFERENCE_DEFAULT_PRESET": "",  # No auto-load, load on demand
                },
                "depends_on": [],
            },
            "llm-gateway": {
                "path": "llm-gateway",
                "port": 8080,
                "health_endpoint": "/health",
                "start": {
                    "docker": "docker compose up -d",
                },
                "depends_on": ["inference"],
            },
            "semantic-search": {
                "path": "semantic-search-service",
                "port": 8084,
                "health_endpoint": "/health",
                "start": {
                    "docker": "docker compose up -d",
                },
                "depends_on": ["qdrant"],
            },
            "code-orchestrator": {
                "path": "Code-Orchestrator-Service",
                "port": 8083,
                "health_endpoint": "/health",
                "start": {
                    "docker": "docker compose up -d",
                },
                "depends_on": ["llm-gateway", "semantic-search"],
            },
        },
    }
    
    TOPOLOGY_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    with open(TOPOLOGY_FILE, "w") as f:
        yaml.dump(topology, f, default_flow_style=False, sort_keys=False)
    
    print(f"{GREEN}Created {TOPOLOGY_FILE}{NC}")
    print(f"\nEdit this file to customize your platform topology.")
    print(f"Then run: platform up --mode=hybrid")


if __name__ == "__main__":
    main()

"""Run two Release/net10.0 Flamme.Server.Web output directories on one disposable PostgreSQL 16 container."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import time


def capture(*args):
    return subprocess.check_output(args, text=True).strip()


def compile_probe(host, destination):
    dotnet = Path(shutil.which("dotnet")).resolve()
    csc = sorted((dotnet.parent / "sdk").glob("*/Roslyn/bincore/csc.dll"))[-1]
    refs = {}
    for pack in ["Microsoft.NETCore.App.Ref", "Microsoft.AspNetCore.App.Ref"]:
        directory = sorted((dotnet.parent / "packs" / pack).glob("10.*/ref/net10.0"))[-1]
        refs.update({p.name: p for p in directory.glob("*.dll")})
    for path in host.glob("*.dll"):
        refs.setdefault(path.name, path)
    subprocess.run([dotnet, csc, "-nologo", "-langversion:preview", "-nullable:enable", "-optimize+",
                    "-target:library", f"-out:{destination}", *[f"-r:{p}" for p in refs.values()],
                    Path(__file__).resolve().with_name("BulkProbe.cs")], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--order")
    parser.add_argument("--compile-only", action="store_true")
    args = parser.parse_args()
    hosts = {"main": args.main.resolve(), "candidate": args.candidate.resolve()}
    default_order = "main,candidate,candidate,main"
    if args.previous:
        hosts["previous"] = args.previous.resolve()
        default_order = "main,previous,candidate,candidate,previous,main"
    order = (args.order or default_order).split(",")
    password = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="flamme-review-bulk-run-") as temp:
        root = Path(temp)
        hook = root / "BulkProbe.dll"
        compile_probe(hosts["main"], hook)
        if args.compile_only:
            return
        cid = subprocess.check_output(["docker", "run", "--rm", "-d", "-p", "127.0.0.1::5432",
                                       "-e", "POSTGRES_PASSWORD", "postgres:16"],
                                      text=True, env=dict(os.environ, POSTGRES_PASSWORD=password)).strip()
        try:
            port = capture("docker", "port", cid, "5432/tcp").split(":")[-1]
            for _ in range(180):
                if subprocess.run(["docker", "exec", cid, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                                  capture_output=True).returncode == 0:
                    break
                time.sleep(.3)
            else:
                raise RuntimeError("PostgreSQL startup timeout")
            for run, label in enumerate(order):
                database = f"bulk{run}"
                subprocess.run(["docker", "exec", cid, "createdb", "-U", "postgres", database], check=True)
                host = hosts[label]
                content = root / f"h{run}"
                content.mkdir()
                for settings in host.glob("appsettings*.json"):
                    shutil.copy(settings, content / settings.name)
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    web_port = sock.getsockname()[1]
                conn = f"Host=127.0.0.1;Port={port};Database={database};Username=postgres;Password={password}"
                env = dict(os.environ, ConnectionStrings__PostgreSql=conn,
                           ASPNETCORE_ENVIRONMENT="Development", DOTNET_ENVIRONMENT="Development",
                           ASPNETCORE_URLS=f"http://127.0.0.1:{web_port}", DOTNET_STARTUP_HOOKS=str(hook),
                           Scripting__Python__AllowHostCodeExecution="false", Logging__LogLevel__Default="Warning",
                           Identity__Tokens__SigningKey=secrets.token_hex(32))
                for i in range(3):
                    env[f"Multitenancy__Tenants__{i}__Configuration__ConnectionStrings__PostgreSql"] = conn
                env["Logging__LogLevel__Microsoft.EntityFrameworkCore.Database.Command"] = "Warning"
                blobs = {name: hashlib.sha256((host / name).read_bytes()).hexdigest()
                         for name in ["Flamme.Persistence.EFCore.Common.dll", "Flamme.Persistence.EFCore.dll", "Flamme.Common.dll", "Flamme.Server.Web.dll"]}
                print(json.dumps(dict(kind="begin", run=run, label=label, blobs=blobs)), flush=True)
                result = subprocess.run(["dotnet", host / "Flamme.Server.Web.dll"], env=env, cwd=content,
                                        capture_output=True, text=True, timeout=240)
                events = [json.loads(s[5:].replace(password, "<private>"))
                          for s in result.stdout.splitlines() if s.startswith("BULK ")]
                for event in events:
                    print(json.dumps(dict(event, run=run, label=label)), flush=True)
                if result.returncode or not events or events[-1].get("kind") != "pass":
                    diagnostics = (result.stdout + result.stderr).replace(password, "<private>").splitlines()[-20:]
                    raise RuntimeError("\n".join(diagnostics))
        finally:
            subprocess.run(["docker", "stop", "-t", "1", cid], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()

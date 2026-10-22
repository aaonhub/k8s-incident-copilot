"""Deterministic layer: snapshot -> citable artifacts + detector signals.

Nothing in this file uses a model. It turns raw API objects into short, stable,
citable text artifacts (each with an ID the model must cite), and runs plain
detectors over them. The detectors serve three jobs:
  1. the "rules" baseline in the eval;
  2. the fact sheet the model starts from;
  3. evidence gates: a model diagnosis is only actionable if the raw facts
     contain the evidence that diagnosis requires (see guard.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

# --------------------------------------------------------------------- utils
ERR_RATIO = 'shop_error_ratio{window="60s"}'


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _age(now: datetime, s: str | None) -> str:
    t = _ts(s)
    if not t:
        return "?"
    sec = max(0, int((now - t).total_seconds()))
    if sec < 120:
        return f"{sec}s"
    if sec < 7200:
        return f"{sec // 60}m"
    return f"{sec // 3600}h"


def _age_seconds(now: datetime, s: str | None) -> float | None:
    t = _ts(s)
    return None if not t else (now - t).total_seconds()


def parse_quantity(q: str | None) -> float | None:
    """CPU -> cores, memory -> bytes. Enough of the k8s quantity grammar for this demo."""
    if q is None:
        return None
    q = str(q)
    units = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "k": 1e3, "M": 1e6, "G": 1e9, "m": 1e-3, "n": 1e-9, "u": 1e-6}
    for u, mult in units.items():
        if q.endswith(u):
            return float(q[: -len(u)]) * mult
    return float(q)


def parse_prom(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in (text or "").splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, val = line.rpartition(" ")
        key = re.sub(r'role="[^"]*",?', "", name).replace("{}", "")
        try:
            out[key] = float(val)
        except ValueError:
            pass
    return out


# ----------------------------------------------------------------- artifacts


@dataclass
class Artifact:
    id: str
    kind: str
    text: str
    subject: str = ""  # e.g. deployment/webapp

    def render(self) -> str:
        return f"[{self.id}] {self.text}"


@dataclass
class Signal:
    kind: str
    subject: str  # deployment/<name> or service/<name>
    detail: str
    artifact_ids: list[str] = field(default_factory=list)

    def render(self) -> str:
        return f"- {self.kind} on {self.subject}: {self.detail} (see {', '.join(self.artifact_ids)})"


class Facts:
    """All artifacts + signals for one snapshot."""

    def __init__(self, snap: dict):
        self.snap = snap
        self.ns = snap["namespace"]
        self.now = _ts(snap["captured_at"])
        self.artifacts: dict[str, Artifact] = {}
        self.signals: list[Signal] = []
        self._index_owners()
        self._build()
        self._detect()

    # ---------------------------------------------------------------- owners
    def _index_owners(self) -> None:
        self.rs_to_deploy: dict[str, str] = {}
        self.rs_revision: dict[str, int] = {}
        for rs in self.snap["replicasets"]:
            md = rs["metadata"]
            for o in md.get("ownerReferences") or []:
                if o["kind"] == "Deployment":
                    self.rs_to_deploy[md["name"]] = o["name"]
            self.rs_revision[md["name"]] = int((md.get("annotations") or {}).get("deployment.kubernetes.io/revision", 0))
        self.current_rs: dict[str, str] = {}
        for rs_name, dep in self.rs_to_deploy.items():
            cur = self.current_rs.get(dep)
            if cur is None or self.rs_revision[rs_name] > self.rs_revision[cur]:
                self.current_rs[dep] = rs_name

    def pod_deployment(self, pod: dict) -> str | None:
        for o in pod["metadata"].get("ownerReferences") or []:
            if o["kind"] == "ReplicaSet":
                return self.rs_to_deploy.get(o["name"])
        return None

    def pod_is_current(self, pod: dict) -> bool:
        for o in pod["metadata"].get("ownerReferences") or []:
            if o["kind"] == "ReplicaSet":
                dep = self.rs_to_deploy.get(o["name"])
                return dep is not None and self.current_rs.get(dep) == o["name"]
        return True

    def add(self, a: Artifact) -> Artifact:
        self.artifacts[a.id] = a
        return a

    # ----------------------------------------------------------------- build
    def _build(self) -> None:
        now = self.now
        self.pod_metrics = {
            m["metadata"]["name"]: {c["name"]: c["usage"] for c in m.get("containers", [])}
            for m in self.snap.get("pod_metrics") or []
        }
        for pod in self.snap["pods"]:
            self.add(self._pod_artifact(pod))
            name = pod["metadata"]["name"]
            if name in self.snap.get("app_metrics", {}) or name in self.pod_metrics:
                self.add(self._metrics_artifact(pod))
        for key, text in sorted(self.snap.get("logs", {}).items()):
            self.add(Artifact(f"log/{key}", "log", text.rstrip(), self._subject_of_pod(key.split("/")[0])))
        for dep in self.snap["deployments"]:
            self.add(self._deploy_artifact(dep))
            ra = self._rollout_artifact(dep)
            if ra:
                self.add(ra)
        for svc in self.snap["services"]:
            self.add(self._service_artifact(svc))
        # Events: dedupe by (object, reason, message); newest first.
        grouped: dict[tuple, dict] = {}
        for ev in self.snap["events"]:
            io = ev["involvedObject"]
            key = (io["kind"], io["name"], ev.get("reason"), ev.get("message"))
            last = ev.get("lastTimestamp") or ev.get("eventTime") or (ev.get("metadata") or {}).get("creationTimestamp")
            first = ev.get("firstTimestamp") or ev.get("eventTime") or last
            count = ev.get("count") or ((ev.get("series") or {}).get("count")) or 1
            g = grouped.setdefault(key, {"type": ev.get("type"), "count": 0, "first": first, "last": last})
            g["count"] += count
            if last and (not g["last"] or last > g["last"]):
                g["last"] = last
            if first and (not g["first"] or first < g["first"]):
                g["first"] = first
        ordered = sorted(grouped.items(), key=lambda kv: kv[1]["last"] or "", reverse=True)
        pod_names = {p["metadata"]["name"] for p in self.snap["pods"]}
        for i, ((kind, name, reason, msg), g) in enumerate(ordered, 1):
            gone = kind == "Pod" and name not in pod_names
            text = (
                f"{g['type']} {reason} {kind.lower()}/{name}"
                f"{' (object no longer exists)' if gone else ''}: {msg} "
                f"[x{g['count']}, first {_age(now, g['first'])} ago, last {_age(now, g['last'])} ago]"
            )
            subj = self._subject_of_pod(name) if kind == "Pod" else f"{kind.lower()}/{name}"
            a = self.add(Artifact(f"event/{i}", "event", text, subj))
            a.meta = {"type": g["type"], "reason": reason, "kind": kind, "name": name, "gone": gone,  # type: ignore[attr-defined]
                      "last_age_s": _age_seconds(now, g["last"]), "message": msg or ""}

    def _subject_of_pod(self, pod_name: str) -> str:
        for p in self.snap["pods"]:
            if p["metadata"]["name"] == pod_name:
                d = self.pod_deployment(p)
                return f"deployment/{d}" if d else f"pod/{pod_name}"
        # pod is gone: infer from the name (deploy-rshash-podhash)
        parts = pod_name.rsplit("-", 2)
        return f"deployment/{parts[0]}" if len(parts) == 3 else f"pod/{pod_name}"

    def _pod_artifact(self, pod: dict) -> Artifact:
        md, st, now = pod["metadata"], pod.get("status") or {}, self.now
        cs_list = st.get("containerStatuses") or []
        ready = sum(1 for c in cs_list if c.get("ready"))
        dep = self.pod_deployment(pod)
        gen = "current" if self.pod_is_current(pod) else "previous"
        parts = [
            f"pod {md['name']} (deployment {dep}, {gen} revision) phase={st.get('phase')} "
            f"ready={ready}/{len(pod['spec']['containers'])} age={_age(now, md.get('creationTimestamp'))}"
        ]
        specs = {c["name"]: c for c in pod["spec"]["containers"]}
        for cs in cs_list:
            s = cs.get("state") or {}
            line = f"container {cs['name']} image={cs.get('image')} restarts={cs.get('restartCount', 0)}"
            if s.get("waiting"):
                w = s["waiting"]
                line += f" state=waiting reason={w.get('reason')} message={w.get('message')!r}"
            elif s.get("running"):
                line += f" state=running since {_age(now, s['running'].get('startedAt'))} ago"
            elif s.get("terminated"):
                t = s["terminated"]
                line += f" state=terminated reason={t.get('reason')} exitCode={t.get('exitCode')}"
            lt = (cs.get("lastState") or {}).get("terminated")
            if lt:
                line += (f"; lastState=terminated reason={lt.get('reason')} exitCode={lt.get('exitCode')} "
                         f"finished {_age(now, lt.get('finishedAt'))} ago")
            spec = specs.get(cs["name"], {})
            res = spec.get("resources") or {}
            if res.get("limits"):
                line += f"; limits={res['limits']}"
            rp = (spec.get("readinessProbe") or {}).get("httpGet")
            if rp:
                line += f"; readinessProbe=GET {rp.get('path')}:{rp.get('port')}"
            parts.append(line)
        for cond in st.get("conditions") or []:
            if cond.get("status") != "True" and cond.get("reason"):
                parts.append(f"condition {cond['type']}=False reason={cond.get('reason')} {cond.get('message') or ''}".strip())
        return Artifact(f"pod/{md['name']}", "pod", "; ".join(parts), f"deployment/{dep}" if dep else f"pod/{md['name']}")

    def _metrics_artifact(self, pod: dict) -> Artifact:
        name = pod["metadata"]["name"]
        prom = parse_prom(self.snap.get("app_metrics", {}).get(name, ""))
        usage = self.pod_metrics.get(name, {})
        parts = [f"metrics for pod {name}"]
        for c in pod["spec"]["containers"]:
            u = usage.get(c["name"], {})
            lim = (c.get("resources") or {}).get("limits") or {}
            if u:
                cpu_m = (parse_quantity(u.get("cpu")) or 0) * 1000
                mem_mi = (parse_quantity(u.get("memory")) or 0) / 2**20
                parts.append(f"cpu usage {cpu_m:.0f}m (limit {lim.get('cpu')}), memory {mem_mi:.0f}Mi (limit {lim.get('memory')})")
        if prom:
            per, thr = prom.get("container_cpu_nr_periods", 0), prom.get("container_cpu_nr_throttled", 0)
            if per:
                parts.append(f"cpu throttled in {int(thr)}/{int(per)} CFS periods ({100 * thr / per:.0f}%)")
            p50 = prom.get('shop_request_seconds{quantile="0.5",window="60s"}')
            p95 = prom.get('shop_request_seconds{quantile="0.95",window="60s"}')
            if p50 is not None:
                parts.append(f"request latency p50={p50 * 1000:.0f}ms p95={p95 * 1000:.0f}ms (60s window)")
            if "shop_requests_total" in prom:
                parts.append(
                    f"requests_total={int(prom['shop_requests_total'])} errors_total={int(prom.get('shop_errors_total', 0))} "
                    f"upstream_errors_total={int(prom.get('shop_upstream_errors_total', 0))} "
                    f"error_ratio_60s={prom.get(ERR_RATIO, 0):.2f}"
                )
        a = Artifact(f"metrics/{name}", "metrics", "; ".join(parts), self._subject_of_pod(name))
        a.meta = prom  # type: ignore[attr-defined]
        return a

    def _deploy_artifact(self, dep: dict) -> Artifact:
        md, sp, st = dep["metadata"], dep["spec"], dep.get("status") or {}
        rev = (md.get("annotations") or {}).get("deployment.kubernetes.io/revision")
        text = (
            f"deployment {md['name']} revision={rev} desired={sp.get('replicas')} updated={st.get('updatedReplicas', 0)} "
            f"ready={st.get('readyReplicas', 0)} available={st.get('availableReplicas', 0)} "
            f"unavailable={st.get('unavailableReplicas', 0)}"
        )
        for c in st.get("conditions") or []:
            text += f"; {c['type']}={c['status']} reason={c.get('reason')}"
        return Artifact(f"deploy/{md['name']}", "deployment", text, f"deployment/{md['name']}")

    @staticmethod
    def template_summary(tpl: dict) -> dict[str, str]:
        out: dict[str, str] = {}
        for c in tpl["spec"]["containers"]:
            p = c["name"]
            out[f"{p}.image"] = c.get("image", "")
            for e in c.get("env") or []:
                if "value" in e:
                    out[f"{p}.env.{e['name']}"] = e["value"]
                elif e.get("valueFrom", {}).get("secretKeyRef"):
                    ref = e["valueFrom"]["secretKeyRef"]
                    out[f"{p}.env.{e['name']}"] = f"<secret {ref.get('name')}/{ref.get('key')}>"
            for kind in ("limits", "requests"):
                for k, v in ((c.get("resources") or {}).get(kind) or {}).items():
                    out[f"{p}.resources.{kind}.{k}"] = str(v)
            rp = (c.get("readinessProbe") or {}).get("httpGet")
            if rp:
                out[f"{p}.readinessProbe.path"] = rp.get("path", "")
        return out

    def _rollout_artifact(self, dep: dict) -> Artifact | None:
        name = dep["metadata"]["name"]
        rss = sorted(
            (rs for rs in self.snap["replicasets"] if self.rs_to_deploy.get(rs["metadata"]["name"]) == name),
            key=lambda rs: self.rs_revision[rs["metadata"]["name"]],
        )
        if not rss:
            return None
        cur = rss[-1]
        lines = [f"rollout history for deployment {name}: " + ", ".join(
            f"revision {self.rs_revision[r['metadata']['name']]} created {_age(self.now, r['metadata'].get('creationTimestamp'))} ago"
            f" (replicas={r.get('status', {}).get('replicas', 0)})" for r in rss)]
        if len(rss) > 1:
            prev = rss[-2]
            a, b = self.template_summary(prev["spec"]["template"]), self.template_summary(cur["spec"]["template"])
            diffs = [f"{k}: {a.get(k)!r} -> {b.get(k)!r}" for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
            # `kubectl rollout restart` only changes an annotation; say so instead of "no diff".
            ann_a = (prev["spec"]["template"]["metadata"].get("annotations") or {}).get("kubectl.kubernetes.io/restartedAt")
            ann_b = (cur["spec"]["template"]["metadata"].get("annotations") or {}).get("kubectl.kubernetes.io/restartedAt")
            if ann_a != ann_b:
                diffs.append("template annotation kubectl.kubernetes.io/restartedAt changed (a rollout restart)")
            lines.append(
                f"change from revision {self.rs_revision[prev['metadata']['name']]} to "
                f"{self.rs_revision[cur['metadata']['name']]}: " + ("; ".join(diffs) if diffs else "no template change")
            )
        a = Artifact(f"rollout/{name}", "rollout", " | ".join(lines), f"deployment/{name}")
        a.meta = {"revisions": len(rss), "current_age_s": _age_seconds(self.now, cur["metadata"].get("creationTimestamp"))}  # type: ignore[attr-defined]
        return a

    def _service_artifact(self, svc: dict) -> Artifact:
        name = svc["metadata"]["name"]
        sel = svc["spec"].get("selector") or {}
        matching = [p for p in self.snap["pods"] if sel and all((p["metadata"].get("labels") or {}).get(k) == v for k, v in sel.items())]
        ready = notready = 0
        for es in self.snap.get("endpointslices") or []:
            if (es["metadata"].get("labels") or {}).get("kubernetes.io/service-name") != name:
                continue
            for ep in es.get("endpoints") or []:
                if (ep.get("conditions") or {}).get("ready"):
                    ready += 1
                else:
                    notready += 1
        names = sorted({(p["metadata"].get("labels") or {}).get("app.kubernetes.io/name", "?") for p in self.snap["pods"]})
        text = (f"service {name} selector={sel} -> pods matching selector={len(matching)}, "
                f"endpoints ready={ready} notReady={notready}; app.kubernetes.io/name labels present on pods in namespace: {names}")
        a = Artifact(f"svc/{name}", "service", text, f"service/{name}")
        a.meta = {"selector": sel, "matching": len(matching), "ready": ready, "notready": notready}  # type: ignore[attr-defined]
        return a

    # ---------------------------------------------------------------- detect
    def _sig(self, kind: str, subject: str, detail: str, ids: list[str]) -> None:
        for s in self.signals:
            if s.kind == kind and s.subject == subject:
                for i in ids:
                    if i not in s.artifact_ids:
                        s.artifact_ids.append(i)
                return
        self.signals.append(Signal(kind, subject, detail, list(ids)))

    def _detect(self) -> None:
        self.unready_pods = {
            p["metadata"]["name"] for p in self.snap["pods"]
            if not all(c.get("ready") for c in (p.get("status") or {}).get("containerStatuses") or [{}])
        }
        for pod in self.snap["pods"]:
            name = pod["metadata"]["name"]
            dep = self.pod_deployment(pod)
            subj = f"deployment/{dep}" if dep else f"pod/{name}"
            pid = f"pod/{name}"
            st = pod.get("status") or {}
            for cs in st.get("containerStatuses") or []:
                w = (cs.get("state") or {}).get("waiting") or {}
                lt = (cs.get("lastState") or {}).get("terminated") or {}
                cur_t = (cs.get("state") or {}).get("terminated") or {}
                reason = w.get("reason")
                if reason in ("ErrImagePull", "ImagePullBackOff", "InvalidImageName"):
                    self._sig("image_pull_failure", subj, f"{reason} for {cs.get('image')}", [pid])
                elif reason == "CreateContainerConfigError":
                    self._sig("container_config_error", subj, w.get("message") or reason, [pid])
                if lt.get("reason") == "OOMKilled" or cur_t.get("reason") == "OOMKilled":
                    self._sig("oom_killed", subj, "container terminated with reason OOMKilled (exit 137)", [pid])
                elif reason == "CrashLoopBackOff" or (cs.get("restartCount", 0) > 0 and lt.get("exitCode") not in (None, 0, 137)):
                    self._sig("crash_loop", subj, f"restarts={cs.get('restartCount')} last exitCode={lt.get('exitCode')}", [pid])
                    prev_key = f"log/{name}/{cs['name']}/previous"
                    if prev_key in self.artifacts:
                        self.signals[-1].artifact_ids.append(prev_key) if prev_key not in self.signals[-1].artifact_ids else None
                running = (cs.get("state") or {}).get("running")
                if running and not cs.get("ready") and (_age_seconds(self.now, running.get("startedAt")) or 0) > 30:
                    self._sig("not_ready", subj, "container running >30s but not Ready", [pid])
        for a in self.artifacts.values():
            meta = getattr(a, "meta", None)
            if a.kind == "event" and meta and meta["type"] == "Warning" and not meta["gone"] and (meta["last_age_s"] or 0) < 300:
                if (meta["reason"] == "Unhealthy" and "Readiness probe failed" in meta["message"]
                        and meta["kind"] == "Pod" and meta["name"] in self.unready_pods):
                    self._sig("readiness_probe_failing", a.subject, meta["message"][:120], [a.id])
                if meta["reason"] == "Unhealthy" and "Liveness probe failed" in meta["message"]:
                    self._sig("liveness_probe_failing", a.subject, meta["message"][:120], [a.id])
            if a.kind == "service" and meta and meta["selector"] and meta["matching"] == 0:
                self._sig("service_no_endpoints", f"service/{a.id.split('/', 1)[1]}",
                          f"selector {meta['selector']} matches 0 pods", [a.id])
            if a.kind == "metrics" and meta:
                per, thr = meta.get("container_cpu_nr_periods", 0), meta.get("container_cpu_nr_throttled", 0)
                if per >= 100 and thr / per > 0.5:
                    self._sig("cpu_throttled", a.subject, f"throttled in {100 * thr / per:.0f}% of CFS periods", [a.id])
                p95 = meta.get('shop_request_seconds{quantile="0.95",window="60s"}', 0)
                if p95 > 0.5:
                    self._sig("high_latency", a.subject, f"p95 latency {p95 * 1000:.0f}ms", [a.id])
                if meta.get(ERR_RATIO, 0) > 0.2:
                    self._sig("high_error_ratio", a.subject, f"error ratio {meta[ERR_RATIO]:.2f}", [a.id])
            if a.kind == "log" and not a.id.endswith("/previous"):
                recent = self._recent_lines(a.text, 60)
                n_to = sum("timed out" in line for line in recent)
                n_fail = sum("request failed" in line or "Connection refused" in line or "-> HTTP 5" in line for line in recent)
                if n_to >= 3:
                    self._sig("upstream_timeouts", a.subject, f"{n_to} upstream timeout lines in the last 60s", [a.id])
                if n_fail >= 3:
                    self._sig("upstream_failures", a.subject, f"{n_fail} failed-request lines in the last 60s", [a.id])
            if a.kind == "rollout" and meta and meta["revisions"] > 1 and (meta["current_age_s"] or 1e9) < 1800:
                self._sig("recent_rollout", a.subject, "new revision rolled out in the last 30 min", [a.id])

    def _recent_lines(self, text: str, seconds: int) -> list[str]:
        """Log lines stamped within `seconds` of the snapshot (the app logs UTC timestamps)."""
        out = []
        for line in text.splitlines():
            try:
                t = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=self.now.tzinfo)
            except ValueError:
                continue
            if (self.now - t).total_seconds() <= seconds:
                out.append(line)
        return out

    # ---------------------------------------------------------------- render
    def fact_sheet(self, max_log_lines: int = 6) -> str:
        """Compact starting context for the model. Full detail is behind tools."""
        out = [f"Namespace: {self.ns}   Snapshot taken: {self.snap['captured_at']}", "",
               "## Detector signals (deterministic, may be incomplete)"]
        out += [s.render() for s in self.signals] or ["- none fired"]
        out += ["", "## Workloads"]
        for kind in ("deployment", "rollout", "service", "pod", "metrics"):
            out += [a.render() for a in self.artifacts.values() if a.kind == kind]
        out += ["", "## Warning events (newest first)"]
        warn = [a for a in self.artifacts.values() if a.kind == "event" and getattr(a, "meta", {}).get("type") == "Warning"]
        out += [a.render() for a in warn[:15]] or ["- none"]
        out += ["", f"## Log tails (last {max_log_lines} lines; use get_logs for more)"]
        for a in self.artifacts.values():
            if a.kind == "log":
                lines = a.text.splitlines()[-max_log_lines:]
                out.append(f"[{a.id}]\n" + "\n".join("    " + line for line in lines))
        return "\n".join(out)

import statistics
import urllib.request
import time

def ping_host(host, count=20):
    latencies = []
    print(f"Pinging {host} ({count} times)...\n")
    for i in range(count):
        start = time.perf_counter()
        try:
            urllib.request.urlopen(f"https://{host}", timeout=5)
            elapsed = (time.perf_counter() - start) * 1000
            latencies.append(elapsed)
            print(f"  [{i+1:02d}] {elapsed:.1f} ms")
        except Exception as e:
            print(f"  [{i+1:02d}] FAILED — {e}")
        time.sleep(0.3)
    return latencies

def report(latencies, host):
    if not latencies:
        print(f"\n❌ No successful pings to {host}")
        return
    avg = statistics.mean(latencies)
    mn  = min(latencies)
    mx  = max(latencies)
    print(f"\n── {host} ──")
    print(f"  Min:  {mn:.1f} ms")
    print(f"  Avg:  {avg:.1f} ms")
    print(f"  Max:  {mx:.1f} ms")
    if avg < 50:
        print(f"  Status: ✅ GOOD (avg < 50ms)")
    elif avg < 150:
        print(f"  Status: ⚠️  ACCEPTABLE (avg < 150ms)")
    else:
        print(f"  Status: 🔴 HIGH LATENCY — install CAT6 cable")

if __name__ == "__main__":
    print("=" * 45)
    print("  MORNING BEETLE — Network Benchmark")
    print("=" * 45 + "\n")

    hosts = ["kite.zerodha.com", "api.kite.trade"]
    results = {}
    for host in hosts:
        results[host] = ping_host(host)
        report(results[host], host)

    print("\n" + "=" * 45)
    print("Benchmark complete. Record results in Phase 1 notes.")
    print("=" * 45)
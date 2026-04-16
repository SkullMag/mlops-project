import time
import requests
import concurrent.futures

URL = "http://localhost:8000/predict"
PAYLOAD = {"request_id": "req_0001", "image_uri": "s3://immich-uploads/img_123.jpg"}
HEADERS = {"Content-Type": "application/json"}
NUM_REQUESTS = 500  # Total requests to send
CONCURRENCY = 50    # Number of simultaneous users

def send_request():
    start = time.time()
    try:
        response = requests.post(URL, json=PAYLOAD, headers=HEADERS, timeout=5)
        is_success = response.status_code == 200
    except Exception:
        is_success = False
    latency = (time.time() - start) * 1000  # Convert to milliseconds
    return latency, is_success

print(f"Starting load test: {NUM_REQUESTS} requests at concurrency {CONCURRENCY}...")
start_time = time.time()

latencies = []
errors = 0

# Send requests concurrently
with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
    futures = [executor.submit(send_request) for _ in range(NUM_REQUESTS)]
    for future in concurrent.futures.as_completed(futures):
        latency, is_success = future.result()
        latencies.append(latency)
        if not is_success:
            errors += 1

total_time = time.time() - start_time
throughput = NUM_REQUESTS / total_time
error_rate = (errors / NUM_REQUESTS) * 100

# Calculate p50 and p95
latencies.sort()
p50 = latencies[int(len(latencies) * 0.50)]
p95 = latencies[int(len(latencies) * 0.95)]

print("\n" + "=" * 40)
print("📊 BASELINE METRICS FOR YOUR TABLE")
print("=" * 40)
print(f"p50 Latency:   {p50:.2f} ms")
print(f"p95 Latency:   {p95:.2f} ms")
print(f"Throughput:    {throughput:.2f} req/s")
print(f"Error Rate:    {error_rate:.2f} %")
print(f"Concurrency:   {CONCURRENCY}")
print("=" * 40)
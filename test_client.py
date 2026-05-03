from models.client import LlamaClient
from core.orchestrator import Orchestrator
import time
from context.watcher import FileWatcher
from context.embedder import Embedder

embedder = Embedder()

chunks = [
    {
        "id": "test_001",
        "text": "def connect_serial(port, baud_rate): opens a serial connection to the given port",
        "metadata": {"file_path": "test.py", "type": "function"}
    },
    {
        "id": "test_002", 
        "text": "def calculate_pwm(speed): converts speed in m/s to PWM signal for motor driver",
        "metadata": {"file_path": "test.py", "type": "function"}
    },
    {
        "id": "test_003",
        "text": "class SensorInterface: handles all sensor reading and parsing from Arduino",
        "metadata": {"file_path": "test.py", "type": "class"}
    }
]

embedder.index_chunks("test_project", chunks)
print("Indexed 3 chunks")

results = embedder.query("test_project", "how do I open a COM port connection")
for r in results:
    print(f"Score {r['score']:.3f}: {r['text'][:80]}")
def on_change(changed_files):
    print(f"Changed: {changed_files}")

watcher = FileWatcher(r"C:\Users\adnan\Documents\repos", on_change)
watcher.start()
print("Watching... modify a file in your repos folder to test")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    watcher.stop()
    print("Stopped")
kamil = Orchestrator()

print("Kamil: ", end="", flush=True)
for chunk in kamil.chat_stream("In two sentences, what is gradient descent?"):
    print(chunk, end="", flush=True)
print()
print(kamil.chat("What is 5 plus 3?"))
print(kamil.chat("Multiply that result by 2."))
client = LlamaClient()

messages = [
    {"role": "system", "content": "You are Kamil, a local AI system."},
    {"role": "user", "content": "Reply with one sentence confirming you are online."}
]

response = client.chat(messages)
print(response)
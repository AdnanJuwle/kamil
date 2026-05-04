from models.client import LlamaClient
from core.config import DEFAULT_TEMPERATURE
from core.council_loader import load_council, unload_council

ANALYST_URL = "http://127.0.0.1:8080"
ADVOCATE_URL = "http://127.0.0.1:8081"
SYNTHESIZER_URL = "http://127.0.0.1:8082"

ANALYST_PROMPT = """You are the Analyst in a three-part deliberation council.

Your role:
- Decompose the question or problem into its core components
- Identify what is known, what is assumed, and what is uncertain
- Surface the key considerations, tradeoffs, and constraints
- Do not give a final answer or recommendation
- Be precise and structured, but not exhaustive
- Maximum 200 words"""

ADVOCATE_PROMPT = """You are playing the role of a harsh critic in a structured debate exercise.

Your ONLY job is to find fault. You are not trying to be balanced or fair.

Rules:
- Assume the mainstream answer is wrong until proven otherwise
- Find the weakest assumptions and attack them directly
- Point out what everyone is ignoring because it's uncomfortable
- Be blunt. One sentence of brutal criticism is worth more than five sentences of measured analysis.
- Do not acknowledge strengths. That is not your role here.
- Do not hedge. Do not say "however" or "on the other hand".
- End with the single most damning point as a standalone sentence.
Maximum 150 words"""

SYNTHESIZER_PROMPT = """You are the Synthesizer in a three-part deliberation council.

You will receive a question, an Analyst's breakdown, and a Devil's Advocate's challenge.

Your role:
- Produce a final response that genuinely accounts for both perspectives
- Do not simply average them — decide what is actually correct after weighing both
- Be direct about where you land and why
- Acknowledge genuine uncertainty where it exists
- Do not pad. Do not hedge unnecessarily.
- This is the response the user will see"""

class Council:
    def __init__(self):
        self.analyst = LlamaClient(base_url=ANALYST_URL)
        self.advocate = LlamaClient(base_url=ADVOCATE_URL)
        self.synthesizer = LlamaClient(base_url=SYNTHESIZER_URL)

    def deliberate(self, user_message: str, context: str = "") -> dict:
        context_block = f"\n\nRelevant context:\n{context}" if context else ""
        load_council()  # no-op if already running

        # Step 1 — Analyst
        analyst_messages = [
            {"role": "system", "content": ANALYST_PROMPT + context_block},
            {"role": "user", "content": user_message}
        ]
        analyst_output = self.analyst.chat(analyst_messages, temperature=0.3, max_tokens=300)

        # Step 2 — Devil's Advocate
        advocate_messages = [
            {"role": "system", "content": ADVOCATE_PROMPT + context_block},
            {"role": "user", "content": f"Original question: {user_message}\n\nAnalyst's breakdown:\n{analyst_output}"}
        ]
        advocate_output = self.advocate.chat(advocate_messages, temperature=0.95, max_tokens=250)

        # Step 3 — Synthesizer
        synthesizer_messages = [
            {"role": "system", "content": SYNTHESIZER_PROMPT + context_block},
            {"role": "user", "content": f"Original question: {user_message}\n\nAnalyst:\n{analyst_output}\n\nDevil's Advocate:\n{advocate_output}"}
        ]
        synthesizer_output = self.synthesizer.chat(synthesizer_messages, temperature=0.5, max_tokens=600)

            
        result = {
            "analyst": analyst_output,
            "advocate": advocate_output,
            "synthesizer": synthesizer_output,
            "final": synthesizer_output
        }
    
        #unload_council()  # free VRAM after council finishes
        return result


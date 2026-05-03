from context.parser import parse_file
import sys

path = sys.argv[1] if len(sys.argv) > 1 else None
if not path:
    print("Usage: python test_parser.py <path_to_file>")
    exit()

chunks = parse_file(path)
print(f"Found {len(chunks)} chunks\n")
for c in chunks:
    print(f"[{c['metadata']['type']}] {c['metadata']['name']} (lines {c['metadata']['start_line']}-{c['metadata']['end_line']})")
    print(c['text'][:120])
    print()
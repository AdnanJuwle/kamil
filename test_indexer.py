from context.indexer import Indexer

indexer = Indexer()

projects = indexer.list_projects()
print("Projects found:")
for i, p in enumerate(projects):
    print(f"  {i+1}. {p}")

print()
project = input("Enter project name to index: ").strip()

def status(current, total, path):
    print(f"  [{current}/{total}] {path[-60:]}")

print(f"Indexing {project}...")
total = indexer.index_project(project, status_callback=status)
print(f"\nDone. {total} chunks indexed.")

print()
query = input("Enter a query to test retrieval: ").strip()
results = indexer.query(query)
print(f"\nTop results:")
for r in results:
    print(f"  Score {r['score']:.3f} | {r['metadata']['file_path'][-50:]} | {r['metadata']['name']}")
    print(f"  {r['text'][:100]}")
    print()
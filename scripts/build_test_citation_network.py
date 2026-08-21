import os, sys, time, json
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from litreview.citation_network import build_test_citation_network_db

def main():
    prod_db = "/mnt/ripe/literature_analyzer_data/batch_tracking.db"
    test_db = "/home/dudu/GoogleDrive/Antigravity/literature_analyzer/data/test_citation_network.db"
    
    print(f"Starting Local Citation Network MVP build...")
    print(f"  Source Production DB (read-only): {prod_db}")
    print(f"  Target Test DB: {test_db}")
    
    t0 = time.time()
    stats = build_test_citation_network_db(prod_db, test_db)
    elapsed = time.time() - t0
    
    print(f"\nBuild complete in {elapsed:.2f}s!")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    
    db_size = os.path.getsize(test_db)
    print(f"\nGenerated test database: {test_db} ({db_size:,} bytes)")

if __name__ == "__main__":
    main()

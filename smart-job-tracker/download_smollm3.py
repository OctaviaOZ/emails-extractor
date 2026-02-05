#!/usr/bin/env python3
"""
FINAL FIXED VERSION - Handles missing files automatically
"""
from huggingface_hub import snapshot_download, hf_hub_download, list_repo_files
import argparse
import os

def list_available_files(repo_id: str) -> list[str]:
    print(f"🔍 Fetching files from {repo_id}...")
    try:
        files = list_repo_files(repo_id, repo_type="model")
        gguf_files = [f for f in files if f.endswith('.gguf')]
        if gguf_files:
            print("\n✅ Available GGUF files:")
            for f in sorted(gguf_files):
                print(f"  - {f}")
            return gguf_files
        else:
            print("❌ No GGUF files found!")
            return []
    except Exception as e:
        print(f"❌ Error: {e}")
        return []

def download_model(repo_id: str, filename: str = None, local_dir: str = "./models"):
    os.makedirs(local_dir, exist_ok=True)
    
    # FIRST: List files to check what exists
    print("📋 Checking available files...")
    available = list_available_files(repo_id)
    
    if not available:
        print("💡 Try alternative repos:")
        print("   lmstudio-community/SmolLM3-3B-GGUF")
        print("   unsloth/SmolLM3-3B-GGUF")
        return
    
    if filename and filename not in available:
        print(f"❌ '{filename}' not found. Available:")
        for f in available[:5]:  # Show first 5
            print(f"   {f}")
        filename = None  # Fall back to full repo
    
    if filename:
        print(f"⬇️  Downloading {filename}...")
        try:
            file_path = hf_hub_download(
                repo_id=repo_id,
                filename=filename,
                local_dir=local_dir
            )
            print(f"✅ Saved: {file_path}")
        except Exception as e:
            print(f"❌ Download failed: {e}")
    else:
        print(f"⬇️  Downloading FULL repo...")
        snapshot_download(
            repo_id=repo_id,
            local_dir=local_dir
        )
        print(f"✅ All files in {local_dir}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="bartowski/HuggingFaceTB_SmolLM3-3B-GGUF")
    parser.add_argument("--file")
    parser.add_argument("--dir", default="./models")
    parser.add_argument("--list", action="store_true")
    
    args = parser.parse_args()
    
    if args.list:
        list_available_files(args.repo)
    else:
        download_model(args.repo, args.file, args.dir)

#python download_smollm3.py --file SmolLM3-3B-Q4_K_M.gguf
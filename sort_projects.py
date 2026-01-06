import pandas as pd
import os

# === CONFIGURATION ===
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DS_SNAPSHOT_PATH = os.path.join(SCRIPT_DIR, 'vfec_results/ds_snapshot (Copy).csv')
DS_PROJECTS_DIR = os.path.join(SCRIPT_DIR, 'ds_projects')
OUTPUT_CSV = os.path.join(SCRIPT_DIR, 'vfec_results/project_types.csv')

def detect_build_system(repo_path):
    if not os.path.exists(repo_path):
        return "MISSING"
    
    # Check for Modern (CMake)
    if os.path.exists(os.path.join(repo_path, 'CMakeLists.txt')):
        return "MODERN_CMAKE"
    
    # Check for Legacy (Autotools/Make)
    if os.path.exists(os.path.join(repo_path, 'configure')) or \
       os.path.exists(os.path.join(repo_path, 'configure.ac')) or \
       os.path.exists(os.path.join(repo_path, 'autogen.sh')):
        return "LEGACY_AUTOTOOLS"
        
    if os.path.exists(os.path.join(repo_path, 'Makefile')):
        return "LEGACY_MAKE"
        
    return "UNKNOWN"

def main():
    print("Sorting projects by build system...")
    
    if not os.path.exists(DS_SNAPSHOT_PATH):
        print("Snapshot not found!")
        return

    # Read the snapshot to get the list of unique projects
    df = pd.read_csv(DS_SNAPSHOT_PATH)
    unique_projects = df['project'].unique()
    
    project_data = []
    
    modern_count = 0
    legacy_count = 0
    
    for project in unique_projects:
        repo_path = os.path.join(DS_PROJECTS_DIR, project)
        build_type = detect_build_system(repo_path)
        
        project_data.append({
            'project': project,
            'build_type': build_type
        })
        
        if "MODERN" in build_type:
            modern_count += 1
        elif "LEGACY" in build_type:
            legacy_count += 1
            
        print(f"Project: {project: <20} | Type: {build_type}")

    # Save results
    results_df = pd.DataFrame(project_data)
    results_df.to_csv(OUTPUT_CSV, index=False)
    
    print("\n" + "="*40)
    print(f"Total Projects Scanned: {len(unique_projects)}")
    print(f"Modern (CMake) Found:   {modern_count}")
    print(f"Legacy (Auto/Make):     {legacy_count}")
    print(f"Results saved to: {OUTPUT_CSV}")
    print("="*40)
    
    if modern_count >= 20:
        print("\nSUCCESS! You have enough Modern projects for the conference.")
    else:
        print("\nWARNING: You might need to include some Legacy projects.")

if __name__ == "__main__":
    main()
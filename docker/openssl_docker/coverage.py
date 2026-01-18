import common 

def process_commit(commit, coverage=True): 
        logging.info(f"Building {commit[:8]} (Coverage)...")
        clean_repo(PROJECT_DIR)
        run_command(f"git checkout -f {commit}", PROJECT_DIR)
        
        if not configure_openssl(PROJECT_DIR, coverage=coverage): return False
        if not build_openssl(PROJECT_DIR): return False
        
        suite = get_openssl_tests(PROJECT_DIR)
        print(f"\nRunning {len(suite)} tests...")

        commit_results = {
            "hash": commit,
            "tests": []
        }

        pb = ProgressBar(len(suite), step=10)
        for i, t in enumerate(suite):
            pb.set(i)

            test = {
                "name": t['name'],
                "failed": False,
                "cmd": t['cmd'],
                "covered_files": []
            }

            # Clean previous coverage data
            common.run_command("find . -name '*.gcda' -delete", PROJECT_DIR)
            
            # For Coverage, 'make target' is fine as it usually runs the test too or we assume build covers it.
            # But usually we need to RUN it to get coverage.
            # If legacy, running 'make test_name' might NOT run it.
            # Let's force run if legacy.

            if not run_command(test.get('cmd'), PROJECT_DIR):
                if test.get("type") == "legacy":
                    logging.info(f"[Legacy] Running binary for test: {test.get('name')}")
                    if not run_command(test.get('run_bin'), PROJECT_DIR, ignore_errors=True):
                        logging.warning(f"Test Build/Run Failed: {test.get('name')}")
                        test['failed'] = True
                        commit_results['tests'].append(test)
                        continue
                logging.warning(f"Test Build/Run Failed: {test.get('name')}")
                test['failed'] = True
                commit_results['tests'].append(test)
                continue
            
            covered = get_covered_files(PROJECT_DIR)
            test['covered_files'] = covered
            commit_results['tests'].append(test)
            
        return commit_results
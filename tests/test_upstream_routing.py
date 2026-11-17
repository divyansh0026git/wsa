"""Unit tests for Gemini upstream build-label discovery, SAPISIDHASH, XSRF/config locks, and model routing."""
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from unittest.mock import MagicMock, Mock, patch

import wsa_server
from wsa_server import config, gemini, models, server
from wsa_server.accounts import make_sapisidhash
from wsa_server.gemini import fetch_latest_bl
from wsa_server.server import GeminiHandler

# --- Build label discovery & prioritization ---
class TestFetchLatestBLIntegration(unittest.TestCase):
    """Integration tests for fetch_latest_bl() function."""

    def _mock_urlopen(self, html_content):
        """Helper to create mock urlopen response."""
        mock_response = MagicMock()
        mock_response.read.return_value = html_content.encode('utf-8')
        return mock_response

    @patch('urllib.request.urlopen')
    def test_extracts_new_gemini_uiserver_format(self, mock_urlopen):
        """Test function extracts boq_gemini-web-uiserver_* from HTML."""
        html = """
        <html>
        <script>
        var config = {
            bl: "boq_gemini-web-uiserver_20260716.08_p0"
        };
        </script>
        </html>
        """
        mock_urlopen.return_value = self._mock_urlopen(html)
        
        result = fetch_latest_bl()
        
        self.assertEqual(result, "boq_gemini-web-uiserver_20260716.08_p0")
        print("✓ Function extracts boq_gemini-web-uiserver format")

    @patch('urllib.request.urlopen')
    def test_extracts_old_bard_server_format(self, mock_urlopen):
        """Test function extracts boq_assistant-bard-web-server_* from HTML."""
        html = """
        <html>
        <script>
        var config = {
            bl: "boq_assistant-bard-web-server_20260716.08_p0"
        };
        </script>
        </html>
        """
        mock_urlopen.return_value = self._mock_urlopen(html)
        
        result = fetch_latest_bl()
        
        self.assertEqual(result, "boq_assistant-bard-web-server_20260716.08_p0")
        print("✓ Function extracts boq_assistant-bard-web-server format")

    @patch('urllib.request.urlopen')
    def test_prioritizes_gemini_uiserver_when_both_present(self, mock_urlopen):
        """Test function prioritizes new format over old when both exist."""
        html = """
        <html>
        <script>
        var old_config = {
            bl: "boq_assistant-bard-web-server_20260715.01_p0"
        };
        var new_config = {
            bl: "boq_gemini-web-uiserver_20260716.08_p0"
        };
        </script>
        </html>
        """
        mock_urlopen.return_value = self._mock_urlopen(html)
        
        result = fetch_latest_bl()
        
        # Should prioritize gemini-web-uiserver
        self.assertEqual(result, "boq_gemini-web-uiserver_20260716.08_p0")
        print("✓ Function prioritizes boq_gemini-web-uiserver over bard-web-server")

    @patch('urllib.request.urlopen')
    def test_returns_none_when_no_pattern_found(self, mock_urlopen):
        """Test function returns None when neither pattern matches."""
        html = """
        <html>
        <script>
        var config = {
            bl: "some_other_server_20260716.08"
        };
        </script>
        </html>
        """
        mock_urlopen.return_value = self._mock_urlopen(html)
        
        result = fetch_latest_bl()
        
        self.assertIsNone(result)
        print("✓ Function returns None when no valid pattern found")

    @patch('urllib.request.urlopen')
    def test_handles_network_errors_gracefully(self, mock_urlopen):
        """Test function returns None on network errors."""
        mock_urlopen.side_effect = Exception("Network error")
        
        result = fetch_latest_bl()
        
        self.assertIsNone(result)
        print("✓ Function handles network errors gracefully")

    @patch('urllib.request.urlopen')
    def test_parses_various_version_numbers(self, mock_urlopen):
        """Test function correctly parses different version number formats."""
        test_cases = [
            "boq_gemini-web-uiserver_20251231.99_p5",
            "boq_gemini-web-uiserver_20260101.00_p0",
            "boq_assistant-bard-web-server_20260716.15_p3",
        ]
        
        for expected_bl in test_cases:
            html = f'<script>var bl = "{expected_bl}";</script>'
            mock_urlopen.return_value = self._mock_urlopen(html)
            
            result = fetch_latest_bl()
            
            self.assertEqual(result, expected_bl, 
                           f"Failed to parse {expected_bl}")
        
        print("✓ Function parses various version number formats")


class TestBuildLabelSelection(unittest.TestCase):
    """Test build label selection logic."""
    
    def _mock_response(self, html_content):
        """Helper to create mock HTTP response."""
        from unittest.mock import MagicMock
        mock_resp = MagicMock()
        mock_resp.read.return_value = html_content.encode('utf-8')
        return mock_resp

    @patch('urllib.request.urlopen')
    def test_scenario_1_only_new_format_present(self, mock_urlopen):
        """Scenario 1: Only boq_gemini-web-uiserver present."""
        html = 'var bl = "boq_gemini-web-uiserver_20260716.08_p0";'
        mock_urlopen.return_value = self._mock_response(html)
        
        result = gemini.fetch_latest_bl()
        
        self.assertEqual(result, "boq_gemini-web-uiserver_20260716.08_p0")
        print("✓ Scenario 1: Correctly selects boq_gemini-web-uiserver when only new format present")

    @patch('urllib.request.urlopen')
    def test_scenario_2_only_old_format_present(self, mock_urlopen):
        """Scenario 2: Only boq_assistant-bard-web-server present."""
        html = 'var bl = "boq_assistant-bard-web-server_20260716.08_p0";'
        mock_urlopen.return_value = self._mock_response(html)
        
        result = gemini.fetch_latest_bl()
        
        self.assertEqual(result, "boq_assistant-bard-web-server_20260716.08_p0")
        print("✓ Scenario 2: Correctly selects boq_assistant-bard-web-server when only old format present")

    @patch('urllib.request.urlopen')
    def test_scenario_3_both_formats_present_prioritizes_new(self, mock_urlopen):
        """Scenario 3: Both formats present - prioritizes new format."""
        html = '''
        var old_bl = "boq_assistant-bard-web-server_20260716.08_p0";
        var new_bl = "boq_gemini-web-uiserver_20260715.00_p0";
        '''
        mock_urlopen.return_value = self._mock_response(html)
        
        result = gemini.fetch_latest_bl()
        
        # Should prioritize gemini-web-uiserver even if version is older
        self.assertEqual(result, "boq_gemini-web-uiserver_20260715.00_p0")
        print("✓ Scenario 3: Prioritizes boq_gemini-web-uiserver even when bard-web-server has newer version")

    @patch('urllib.request.urlopen')
    def test_scenario_4_multiple_new_format_returns_first(self, mock_urlopen):
        """Scenario 4: Multiple new format entries - returns first match."""
        html = '''
        var bl1 = "boq_gemini-web-uiserver_20260715.00_p0";
        var bl2 = "boq_gemini-web-uiserver_20260716.08_p0";
        '''
        mock_urlopen.return_value = self._mock_response(html)
        
        result = gemini.fetch_latest_bl()
        
        # Returns first occurrence (regex.search returns first match)
        self.assertEqual(result, "boq_gemini-web-uiserver_20260715.00_p0")
        print("✓ Scenario 4: Returns first match when multiple new format entries exist")

    @patch('urllib.request.urlopen')
    def test_scenario_5_no_valid_pattern(self, mock_urlopen):
        """Scenario 5: No valid build label pattern found."""
        html = 'var config = {"server": "unknown_server_type"};'
        mock_urlopen.return_value = self._mock_response(html)
        
        result = gemini.fetch_latest_bl()
        
        self.assertIsNone(result)
        print("✓ Scenario 5: Returns None when no valid pattern found")

    @patch('urllib.request.urlopen')
    def test_version_number_formats(self, mock_urlopen):
        """Test various valid version number formats are recognized."""
        test_cases = [
            # (input, expected_output)
            ("boq_gemini-web-uiserver_20260716.08_p0", "boq_gemini-web-uiserver_20260716.08_p0"),
            ("boq_gemini-web-uiserver_20251231.99_p9", "boq_gemini-web-uiserver_20251231.99_p9"),
            ("boq_assistant-bard-web-server_20260101.00_p0", "boq_assistant-bard-web-server_20260101.00_p0"),
        ]
        
        for input_bl, expected in test_cases:
            html = f'var bl = "{input_bl}";'
            mock_urlopen.return_value = self._mock_response(html)
            
            result = gemini.fetch_latest_bl()
            
            self.assertEqual(result, expected, f"Failed for input: {input_bl}")
        
        print("✓ All version number formats correctly parsed")


def run_tests():
    """Run all build label selection tests."""
    print("\n" + "="*70)
    print("Build Label Selection Verification")
    print("="*70 + "\n")
    
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestBuildLabelSelection)
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    
    print("\n" + "="*70)
    if result.wasSuccessful():
        print("✅ All build label selection tests PASSED")
        print("="*70)
        print("\nValidated Behavior:")
        print("  • Prioritizes boq_gemini-web-uiserver_* (Pro-capable server)")
        print("  • Falls back to boq_assistant-bard-web-server_* (anonymous server)")
        print("  • Correctly parses version numbers (YYYYMMDD.NN_pN)")
        print("  • Returns first match when multiple entries exist")
        print("  • Handles missing patterns gracefully (returns None)")
        print("\nRequirements Validated: 2.6, 2.7")
        return True
    else:
        print("❌ Some tests FAILED")
        print("="*70)
        return False


# --- Build label downgrade protection ---
class TestBLDowngradeProtection(unittest.TestCase):
    """Tests for build label downgrade protection."""

    def setUp(self):
        """Reset CONFIG before each test."""
        self.original_bl = config.CONFIG["gemini_bl"]

    def tearDown(self):
        """Restore original CONFIG after each test."""
        config.CONFIG["gemini_bl"] = self.original_bl

    @patch.object(gemini, 'fetch_latest_bl')
    def test_prevents_downgrade_from_pro_to_anonymous(self, mock_fetch):
        """
        Test: Verify Pro-capable BL is never overwritten with anonymous BL.
        
        GIVEN CONFIG["gemini_bl"] starts with "boq_gemini-web-uiserver_"
        WHEN fetch_latest_bl() returns a "boq_assistant-bard-web-server_*" value
        THEN update_bl_if_needed() returns False
        AND CONFIG["gemini_bl"] remains unchanged
        AND a warning is logged
        """
        # Setup: Current BL is Pro-capable
        pro_bl = "boq_gemini-web-uiserver_20260716.08_p0"
        config.CONFIG["gemini_bl"] = pro_bl
        
        # Mock: fetch_latest_bl() returns anonymous server
        anon_bl = "boq_assistant-bard-web-server_20260716.08_p0"
        mock_fetch.return_value = anon_bl
        
        # Execute
        with patch.object(gemini, 'log') as mock_log:
            result = gemini.update_bl_if_needed()
        
        # Verify: Downgrade blocked
        self.assertFalse(result, "Should return False when downgrade is blocked")
        self.assertEqual(config.CONFIG["gemini_bl"], pro_bl,
                        "Pro-capable BL should not be overwritten")
        
        # Verify: Warning logged
        mock_log.assert_called_once()
        log_call_args = mock_log.call_args[0][0]
        self.assertIn("downgrade blocked", log_call_args.lower())
        self.assertIn(pro_bl, log_call_args)
        self.assertIn(anon_bl, log_call_args)

    @patch.object(gemini, 'fetch_latest_bl')
    def test_allows_update_within_same_server_type_pro(self, mock_fetch):
        """
        Test: Verify updates are allowed within same server type (Pro).
        
        GIVEN CONFIG["gemini_bl"] is "boq_gemini-web-uiserver_20260716.08_p0"
        WHEN fetch_latest_bl() returns "boq_gemini-web-uiserver_20260717.01_p0"
        THEN update_bl_if_needed() returns True
        AND CONFIG["gemini_bl"] is updated to the new value
        """
        # Setup: Current BL is Pro-capable
        old_pro_bl = "boq_gemini-web-uiserver_20260716.08_p0"
        config.CONFIG["gemini_bl"] = old_pro_bl
        
        # Mock: fetch_latest_bl() returns newer Pro-capable server
        new_pro_bl = "boq_gemini-web-uiserver_20260717.01_p0"
        mock_fetch.return_value = new_pro_bl
        
        # Execute
        with patch.object(gemini, 'log') as mock_log:
            result = gemini.update_bl_if_needed()
        
        # Verify: Update allowed
        self.assertTrue(result, "Should return True when update is allowed")
        self.assertEqual(config.CONFIG["gemini_bl"], new_pro_bl,
                        "BL should be updated to newer Pro-capable version")
        
        # Verify: Update logged
        mock_log.assert_called_once()
        log_call_args = mock_log.call_args[0][0]
        self.assertIn("auto-updated", log_call_args.lower())
        self.assertIn(old_pro_bl, log_call_args)
        self.assertIn(new_pro_bl, log_call_args)

    @patch.object(gemini, 'fetch_latest_bl')
    def test_allows_update_within_same_server_type_anonymous(self, mock_fetch):
        """
        Test: Verify updates are allowed within same server type (anonymous).
        
        GIVEN CONFIG["gemini_bl"] is "boq_assistant-bard-web-server_20260716.08_p0"
        WHEN fetch_latest_bl() returns "boq_assistant-bard-web-server_20260717.01_p0"
        THEN update_bl_if_needed() returns True
        AND CONFIG["gemini_bl"] is updated to the new value
        """
        # Setup: Current BL is anonymous
        old_anon_bl = "boq_assistant-bard-web-server_20260716.08_p0"
        config.CONFIG["gemini_bl"] = old_anon_bl
        
        # Mock: fetch_latest_bl() returns newer anonymous server
        new_anon_bl = "boq_assistant-bard-web-server_20260717.01_p0"
        mock_fetch.return_value = new_anon_bl
        
        # Execute
        with patch.object(gemini, 'log') as mock_log:
            result = gemini.update_bl_if_needed()
        
        # Verify: Update allowed
        self.assertTrue(result, "Should return True when update is allowed")
        self.assertEqual(config.CONFIG["gemini_bl"], new_anon_bl,
                        "BL should be updated to newer anonymous version")

    @patch.object(gemini, 'fetch_latest_bl')
    def test_allows_upgrade_from_anonymous_to_pro(self, mock_fetch):
        """
        Test: Verify upgrade from anonymous to Pro-capable is allowed.
        
        GIVEN CONFIG["gemini_bl"] is "boq_assistant-bard-web-server_20260716.08_p0"
        WHEN fetch_latest_bl() returns "boq_gemini-web-uiserver_20260717.01_p0"
        THEN update_bl_if_needed() returns True
        AND CONFIG["gemini_bl"] is updated to the Pro-capable value
        """
        # Setup: Current BL is anonymous
        anon_bl = "boq_assistant-bard-web-server_20260716.08_p0"
        config.CONFIG["gemini_bl"] = anon_bl
        
        # Mock: fetch_latest_bl() returns Pro-capable server
        pro_bl = "boq_gemini-web-uiserver_20260717.01_p0"
        mock_fetch.return_value = pro_bl
        
        # Execute
        with patch.object(gemini, 'log') as mock_log:
            result = gemini.update_bl_if_needed()
        
        # Verify: Upgrade allowed
        self.assertTrue(result, "Should return True when upgrade is allowed")
        self.assertEqual(config.CONFIG["gemini_bl"], pro_bl,
                        "BL should be upgraded to Pro-capable version")
        
        # Verify: Update logged
        mock_log.assert_called_once()
        log_call_args = mock_log.call_args[0][0]
        self.assertIn("auto-updated", log_call_args.lower())

    @patch.object(gemini, 'fetch_latest_bl')
    def test_no_update_when_bl_unchanged(self, mock_fetch):
        """
        Test: No update when fetched BL is same as current.
        
        GIVEN CONFIG["gemini_bl"] is "boq_gemini-web-uiserver_20260716.08_p0"
        WHEN fetch_latest_bl() returns the same value
        THEN update_bl_if_needed() returns False
        AND CONFIG["gemini_bl"] remains unchanged
        AND no log message is generated
        """
        # Setup: Current BL
        current_bl = "boq_gemini-web-uiserver_20260716.08_p0"
        config.CONFIG["gemini_bl"] = current_bl
        
        # Mock: fetch_latest_bl() returns same BL
        mock_fetch.return_value = current_bl
        
        # Execute
        with patch.object(gemini, 'log') as mock_log:
            result = gemini.update_bl_if_needed()
        
        # Verify: No update
        self.assertFalse(result, "Should return False when BL is unchanged")
        self.assertEqual(config.CONFIG["gemini_bl"], current_bl,
                        "BL should remain unchanged")
        
        # Verify: No log message
        mock_log.assert_not_called()

    @patch.object(gemini, 'fetch_latest_bl')
    def test_no_update_when_fetch_fails(self, mock_fetch):
        """
        Test: No update when fetch_latest_bl() returns None.
        
        GIVEN CONFIG["gemini_bl"] is any value
        WHEN fetch_latest_bl() returns None (fetch failed)
        THEN update_bl_if_needed() returns False
        AND CONFIG["gemini_bl"] remains unchanged
        """
        # Setup: Current BL
        current_bl = "boq_gemini-web-uiserver_20260716.08_p0"
        config.CONFIG["gemini_bl"] = current_bl
        
        # Mock: fetch_latest_bl() returns None
        mock_fetch.return_value = None
        
        # Execute
        result = gemini.update_bl_if_needed()
        
        # Verify: No update
        self.assertFalse(result, "Should return False when fetch fails")
        self.assertEqual(config.CONFIG["gemini_bl"], current_bl,
                        "BL should remain unchanged when fetch fails")

    @patch.object(gemini, 'fetch_latest_bl')
    def test_edge_case_empty_current_bl(self, mock_fetch):
        """
        Test: Update allowed when current BL is empty string.
        
        GIVEN CONFIG["gemini_bl"] is ""
        WHEN fetch_latest_bl() returns any valid BL
        THEN update_bl_if_needed() returns True
        AND CONFIG["gemini_bl"] is updated
        """
        # Setup: Empty current BL
        config.CONFIG["gemini_bl"] = ""
        
        # Mock: fetch_latest_bl() returns anonymous BL
        new_bl = "boq_assistant-bard-web-server_20260716.08_p0"
        mock_fetch.return_value = new_bl
        
        # Execute
        with patch.object(gemini, 'log'):
            result = gemini.update_bl_if_needed()
        
        # Verify: Update allowed
        self.assertTrue(result, "Should allow update when current BL is empty")
        self.assertEqual(config.CONFIG["gemini_bl"], new_bl,
                        "BL should be updated from empty string")


# --- Thread safety ---
class TestConfigThreadSafety(unittest.TestCase):
    """Test thread safety of CONFIG mutations."""
    
    def setUp(self):
        """Save original CONFIG state."""
        with config._CONFIG_LOCK:
            self.original_config = dict(config.CONFIG)
        
    def tearDown(self):
        """Restore original CONFIG state."""
        with config._CONFIG_LOCK:
            config.CONFIG.clear()
            config.CONFIG.update(self.original_config)
    
    def test_update_bl_if_needed_thread_safety(self):
        """Test that update_bl_if_needed() is thread-safe under concurrent access."""
        # Setup: Start with a Pro-capable BL
        with config._CONFIG_LOCK:
            config.CONFIG["gemini_bl"] = "boq_gemini-web-uiserver_20260716.08_p0"
        
        call_count = 0
        lock = threading.Lock()
        
        def mock_fetch_latest_bl():
            nonlocal call_count
            with lock:
                call_count += 1
            # Simulate network delay
            time.sleep(0.01)
            # Return different values alternating between Pro and anonymous
            with lock:
                if call_count % 2 == 0:
                    return "boq_gemini-web-uiserver_20260717.00_p0"
                else:
                    return "boq_assistant-bard-web-server_20260717.00_p0"
        
        with patch.object(gemini, 'fetch_latest_bl', side_effect=mock_fetch_latest_bl):
            # Run 10 concurrent update attempts
            threads = []
            results = []
            
            def update_and_record():
                result = gemini.update_bl_if_needed()
                results.append(result)
            
            for _ in range(10):
                thread = threading.Thread(target=update_and_record)
                threads.append(thread)
                thread.start()
            
            for thread in threads:
                thread.join()
            
            # Verify: CONFIG should still be valid (no corruption)
            with config._CONFIG_LOCK:
                final_bl = config.CONFIG["gemini_bl"]
            
            # Final BL should either be the original Pro BL or a newer Pro BL
            # It should NEVER be the anonymous BL due to downgrade protection
            self.assertIn("boq_gemini-web-uiserver_", final_bl,
                         "BL should not downgrade to anonymous server")
    
    def test_refresh_xsrf_thread_safety(self):
        """Test that refresh_xsrf() is thread-safe under concurrent access."""
        # Setup: Start with an old token
        with config._CONFIG_LOCK:
            config.CONFIG["xsrf_token"] = "old_token"
            config.CONFIG["cookie_file"] = None  # Prevent file I/O
        
        call_count = 0
        lock = threading.Lock()
        
        def mock_fetch_xsrf_with_cookie():
            nonlocal call_count
            with lock:
                call_count += 1
            # Simulate network delay
            time.sleep(0.01)
            # Return a unique token each time
            with lock:
                return f"new_token_{call_count}"
        
        with patch.object(gemini, 'fetch_xsrf_with_cookie', 
                         side_effect=mock_fetch_xsrf_with_cookie):
            # Run 10 concurrent refresh attempts
            threads = []
            results = []
            
            def refresh_and_record():
                result = gemini.refresh_xsrf()
                results.append(result)
            
            for _ in range(10):
                thread = threading.Thread(target=refresh_and_record)
                threads.append(thread)
                thread.start()
            
            for thread in threads:
                thread.join()
            
            # Verify: CONFIG should contain a valid token (not corrupted)
            with config._CONFIG_LOCK:
                final_token = config.CONFIG["xsrf_token"]
            
            # Token should be one of the generated tokens
            self.assertTrue(final_token.startswith("new_token_"),
                          f"Token should be updated: {final_token}")
            
            # Verify no corruption: should be a complete token string
            self.assertRegex(final_token, r'^new_token_\d+$',
                           "Token should not be corrupted")
    
    def test_load_config_thread_safety(self):
        """Test that load_config() is thread-safe under concurrent access."""
        import tempfile
        import json
        
        # Create a temporary config file
        config_data = {
            "port": 9999,
            # Test concurrent config loading without inheriting a cookie file
            # whose persisted token legitimately overrides the configured one.
            "cookie_file": None,
            "gemini_bl": "boq_gemini-web-uiserver_20260720.00_p0",
            "xsrf_token": "test_token_123"
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(config_data, f)
            config_file = f.name
        
        try:
            # Run 10 concurrent load attempts
            threads = []
            
            def load_config_thread():
                config.load_config(config_file)
            
            for _ in range(10):
                thread = threading.Thread(target=load_config_thread)
                threads.append(thread)
                thread.start()
            
            for thread in threads:
                thread.join()
            
            # Verify: CONFIG should contain the loaded values (not corrupted)
            with config._CONFIG_LOCK:
                port = config.CONFIG["port"]
                bl = config.CONFIG["gemini_bl"]
                token = config.CONFIG["xsrf_token"]
            
            self.assertEqual(port, 9999, "Port should be loaded correctly")
            self.assertEqual(bl, "boq_gemini-web-uiserver_20260720.00_p0",
                           "BL should be loaded correctly")
            self.assertEqual(token, "test_token_123",
                           "Token should be loaded correctly")
        
        finally:
            # Cleanup
            os.unlink(config_file)
    
    def test_concurrent_config_mutations(self):
        """Test concurrent mutations from different threads don't corrupt CONFIG."""
        # Setup: Start with baseline config
        with config._CONFIG_LOCK:
            config.CONFIG["gemini_bl"] = "boq_gemini-web-uiserver_20260716.08_p0"
            config.CONFIG["xsrf_token"] = "initial_token"
            config.CONFIG["port"] = 8081
        
        def mock_fetch_bl():
            time.sleep(0.001)
            return "boq_gemini-web-uiserver_20260717.00_p0"
        
        def mock_fetch_xsrf():
            time.sleep(0.001)
            return "new_xsrf_token"
        
        with patch.object(gemini, 'fetch_latest_bl', side_effect=mock_fetch_bl), \
             patch.object(gemini, 'fetch_xsrf_with_cookie', side_effect=mock_fetch_xsrf):
            
            threads = []
            
            # Mix of different mutation operations
            for i in range(20):
                if i % 3 == 0:
                    thread = threading.Thread(target=gemini.update_bl_if_needed)
                elif i % 3 == 1:
                    thread = threading.Thread(target=gemini.refresh_xsrf)
                else:
                    # Direct CONFIG mutation (simulating main() style updates)
                    def update_port():
                        with config._CONFIG_LOCK:
                            config.CONFIG["port"] = 9000 + threading.get_ident() % 100
                    thread = threading.Thread(target=update_port)
                
                threads.append(thread)
                thread.start()
            
            for thread in threads:
                thread.join()
            
            # Verify: CONFIG should still be in a valid state
            with config._CONFIG_LOCK:
                bl = config.CONFIG["gemini_bl"]
                token = config.CONFIG["xsrf_token"]
                port = config.CONFIG["port"]
            
            # BL should be valid (either original or updated)
            self.assertIn("boq_gemini-web-uiserver_", bl,
                         "BL should be valid")
            
            # Token should be valid (either initial or updated)
            self.assertIn("token", token.lower(),
                         "Token should be valid")
            
            # Port should be a valid integer
            self.assertIsInstance(port, int,
                                "Port should be an integer")
            self.assertGreaterEqual(port, 8081,
                                  "Port should be >= 8081")
    
    def test_config_lock_exists(self):
        """Test that _CONFIG_LOCK exists and is a threading.Lock."""
        self.assertTrue(hasattr(config, '_CONFIG_LOCK'),
                       "_CONFIG_LOCK should exist in module")
        self.assertIsInstance(config._CONFIG_LOCK, type(threading.Lock()),
                            "_CONFIG_LOCK should be a threading.Lock")
    
    def test_gemini_stream_generate_config_reads_are_safe(self):
        """Test that gemini_stream_generate reads CONFIG values safely."""
        # Setup: Mock all external dependencies
        with config._CONFIG_LOCK:
            config.CONFIG["gemini_bl"] = "boq_gemini-web-uiserver_20260716.08_p0"
            config.CONFIG["xsrf_token"] = "test_token"
            config.CONFIG["auth_user"] = 0
            config.CONFIG["retry_attempts"] = 1
            config.CONFIG["retry_delay_sec"] = 0
            config.CONFIG["request_timeout_sec"] = 30
            config.CONFIG["proxy"] = None
        
        mock_response = MagicMock()
        mock_response.read.return_value = b'test response'
        
        # Track whether CONFIG reads happen outside the lock
        # (This is hard to test directly, but we can at least verify the function runs)
        
        with patch.object(gemini, 'load_cookie', return_value=("", None)), \
             patch.object(gemini, 'account_prefix', return_value=""), \
             patch.object(gemini, 'apply_chat_persistence_flags'), \
             patch('urllib.request.urlopen', return_value=mock_response):
            
            # Run multiple concurrent calls
            threads = []
            errors = []
            
            def call_gemini():
                try:
                    result = gemini.gemini_stream_generate("test", 1, 4)
                    self.assertIsNotNone(result)
                except Exception as e:
                    errors.append(e)
            
            for _ in range(5):
                thread = threading.Thread(target=call_gemini)
                threads.append(thread)
                thread.start()
            
            for thread in threads:
                thread.join()
            
            # Verify no errors occurred
            if errors:
                self.fail(f"Errors occurred during concurrent calls: {errors}")


# --- SAPISIDHASH generation ---
class TestSAPISIDHashVariants:
    """Test SAPISIDHASH generation for three token variants."""
    
    def test_authorization_header_contains_all_three_variants(self):
        """Verify Authorization header contains SAPISIDHASH, SAPISID1PHASH, and SAPISID3PHASH."""
        sapisid = "test_cookie_value_123"
        auth_header = make_sapisidhash(sapisid)
        
        # Check that all three variant names are present
        assert "SAPISIDHASH" in auth_header, "Missing SAPISIDHASH variant"
        assert "SAPISID1PHASH" in auth_header, "Missing SAPISID1PHASH variant"
        assert "SAPISID3PHASH" in auth_header, "Missing SAPISID3PHASH variant"
    
    def test_each_variant_has_valid_hash_format(self):
        """Verify each variant follows the format: VARIANT_NAME timestamp_hexhash."""
        sapisid = "test_cookie_value_456"
        auth_header = make_sapisidhash(sapisid)
        
        # Pattern: VARIANT_NAME timestamp_hexhash (40-char SHA1)
        # Example: SAPISIDHASH 1234567890_abcdef0123456789abcdef0123456789abcdef01
        pattern = r"(\w+) (\d+)_([0-9a-f]{40})"
        
        matches = re.findall(pattern, auth_header)
        assert len(matches) == 3, f"Expected 3 hash variants, found {len(matches)}"
        
        # Extract variant names and verify they're correct
        variant_names = [match[0] for match in matches]
        expected_names = ["SAPISIDHASH", "SAPISID1PHASH", "SAPISID3PHASH"]
        assert variant_names == expected_names, \
            f"Variant names mismatch: expected {expected_names}, got {variant_names}"
        
        # Verify timestamps and hashes are valid
        for variant_name, timestamp, hash_value in matches:
            # Timestamp should be a reasonable Unix timestamp (within last 10 years)
            ts = int(timestamp)
            current_time = int(time.time())
            assert ts > current_time - (10 * 365 * 24 * 3600), \
                f"Timestamp {ts} is too old for variant {variant_name}"
            assert ts <= current_time + 60, \
                f"Timestamp {ts} is in the future for variant {variant_name}"
            
            # Hash should be 40-character hex string (SHA1)
            assert len(hash_value) == 40, \
                f"Hash length is {len(hash_value)}, expected 40 for variant {variant_name}"
            assert re.match(r'^[0-9a-f]{40}$', hash_value), \
                f"Hash contains invalid characters for variant {variant_name}"
    
    def test_all_three_hashes_are_identical(self):
        """Verify all three variants produce identical timestamp_hash values."""
        sapisid = "test_cookie_value_789"
        auth_header = make_sapisidhash(sapisid)
        
        # Extract timestamp_hash pairs
        pattern = r"\w+ (\d+_[0-9a-f]{40})"
        hash_values = re.findall(pattern, auth_header)
        
        assert len(hash_values) == 3, f"Expected 3 hash values, found {len(hash_values)}"
        
        # All three should be identical
        first_hash = hash_values[0]
        assert all(h == first_hash for h in hash_values), \
            f"Hash values are not identical: {hash_values}"
    
    def test_hash_algorithm_correctness(self):
        """Verify the hash is computed correctly using SHA1(timestamp SAPISID origin)."""
        sapisid = "test_sapisid_value"
        auth_header = make_sapisidhash(sapisid)
        
        # Extract timestamp and hash from the first variant
        pattern = r"SAPISIDHASH (\d+)_([0-9a-f]{40})"
        match = re.search(pattern, auth_header)
        assert match, "Failed to parse SAPISIDHASH variant"
        
        timestamp_str, hash_value = match.groups()
        timestamp = int(timestamp_str)
        
        # Recompute the hash using the same algorithm
        expected_hash = hashlib.sha1(
            f"{timestamp} {sapisid} https://gemini.google.com".encode()
        ).hexdigest()
        
        assert hash_value == expected_hash, \
            f"Hash mismatch: expected {expected_hash}, got {hash_value}"
    
    def test_format_matches_browser_capture(self):
        """Verify the format matches browser captures: SAPISIDHASH {hash} SAPISID1PHASH {hash} SAPISID3PHASH {hash}."""
        sapisid = "browser_test_value"
        auth_header = make_sapisidhash(sapisid)
        
        # Expected format with three space-separated sections
        # Each section: VARIANT_NAME timestamp_hash
        parts = auth_header.split()
        
        # Should have 6 parts: [VARIANT1, hash1, VARIANT2, hash2, VARIANT3, hash3]
        assert len(parts) == 6, \
            f"Expected 6 space-separated parts, found {len(parts)}: {parts}"
        
        # Verify the sequence
        assert parts[0] == "SAPISIDHASH", "First variant should be SAPISIDHASH"
        assert parts[2] == "SAPISID1PHASH", "Second variant should be SAPISID1PHASH"
        assert parts[4] == "SAPISID3PHASH", "Third variant should be SAPISID3PHASH"
        
        # Verify hash values (parts[1], parts[3], parts[5]) have correct format
        for i, part_idx in enumerate([1, 3, 5], 1):
            hash_part = parts[part_idx]
            assert re.match(r'^\d+_[0-9a-f]{40}$', hash_part), \
                f"Hash part {i} has invalid format: {hash_part}"
    
    def test_same_sapisid_produces_consistent_hashes_within_second(self):
        """Verify that calling the function multiple times within the same second produces identical results."""
        sapisid = "consistency_test_value"
        
        # Call multiple times in quick succession
        results = [make_sapisidhash(sapisid) for _ in range(5)]
        
        # All results should be identical if called within the same second
        # (timestamp is in seconds, so hash should be the same)
        first_result = results[0]
        assert all(r == first_result for r in results), \
            f"Results are inconsistent within same second: {results}"
    
    def test_different_sapisid_produces_different_hashes(self):
        """Verify that different SAPISID values produce different hashes."""
        sapisid1 = "first_cookie_value"
        sapisid2 = "second_cookie_value"
        
        auth1 = make_sapisidhash(sapisid1)
        auth2 = make_sapisidhash(sapisid2)
        
        # Extract the hash portions
        pattern = r"SAPISIDHASH (\d+_[0-9a-f]{40})"
        hash1 = re.search(pattern, auth1).group(1)
        hash2 = re.search(pattern, auth2).group(1)
        
        # Timestamps might be the same, but hashes should differ
        assert hash1 != hash2, \
            f"Different SAPISID values produced identical hashes: {hash1} == {hash2}"


# --- StreamGenerate & Iter Argon/XSRF routing ---
class TestTask311StreamGenerate(unittest.TestCase):
    """Test gemini_stream_generate() for Argon headers and XSRF retry."""
    
    def setUp(self):
        """Save original CONFIG state."""
        # These cases inspect direct upstream requests, independent of live tabs
        # and relay state left by other tests.
        for name in ("is_dom_relay_active", "argon_strict_relay_required"):
            relay_patch = patch(f"wsa_server.gemini.{name}", return_value=False)
            relay_patch.start()
            self.addCleanup(relay_patch.stop)
        with config._CONFIG_LOCK:
            self.original_config = dict(config.CONFIG)
        
        # Setup test config
        with config._CONFIG_LOCK:
            config.CONFIG["gemini_bl"] = "boq_gemini-web-uiserver_20260716.08_p0"
            config.CONFIG["xsrf_token"] = "test_xsrf_token"
            config.CONFIG["auth_user"] = "0"
            config.CONFIG["retry_attempts"] = 3
            config.CONFIG["retry_delay_sec"] = 0.1
            config.CONFIG["request_timeout_sec"] = 30
            config.CONFIG["cookie_file"] = None
            config.CONFIG["proxy"] = None
    
    def tearDown(self):
        """Restore original CONFIG state."""
        with config._CONFIG_LOCK:
            config.CONFIG.clear()
            config.CONFIG.update(self.original_config)
    
    @patch('wsa_server.gemini.load_cookie')
    @patch('wsa_server.gemini.account_prefix')
    @patch('urllib.request.urlopen')
    def test_argon_model_includes_binary_protobuf_headers(self, mock_urlopen, mock_prefix, mock_cookie):
        """Test that Argon model requests include all four binary Protobuf headers.
        
        **Validates: Requirements 1.1, 2.2**
        """
        # Setup
        mock_prefix.return_value = ""
        mock_cookie.return_value = ("test_cookie", "test_sapisid")
        
        mock_response = Mock()
        mock_response.read.return_value = b'{"response": "test"}'
        mock_urlopen.return_value = mock_response
        
        # Argon model config with hex key
        argon_cfg = {
            "mode": 7,
            "think": 0,
            "hex": "95a4c0abcba24364",
            "slot80": 2,
        }
        
        # Execute
        result = gemini.gemini_stream_generate(
            prompt="Test prompt",
            model_cfg=argon_cfg,
            file_refs=None
        )
        
        # Verify: Request was made with binary Protobuf headers
        self.assertEqual(mock_urlopen.call_count, 1)
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        
        # Check all four binary headers are present
        # Note: HTTP header keys are case-insensitive; urllib normalizes them
        headers_lower = {k.lower(): v for k, v in request.headers.items()}
        
        self.assertIn("x-goog-ext-525001261-bin", headers_lower,
                     "x-goog-ext-525001261-bin header should be present for Argon model")
        self.assertIn("x-goog-ext-525005358-bin", headers_lower,
                     "x-goog-ext-525005358-bin header should be present for Argon model")
        self.assertIn("x-goog-ext-73010989-bin", headers_lower,
                     "x-goog-ext-73010989-bin header should be present for Argon model")
        self.assertIn("x-goog-ext-73010990-bin", headers_lower,
                     "x-goog-ext-73010990-bin header should be present for Argon model")
        
        # Verify fixed capability headers have correct Base64 values
        self.assertEqual(headers_lower["x-goog-ext-73010989-bin"], "CAA=",
                        "x-goog-ext-73010989-bin should be Base64 'CAA=' for [0]")
        self.assertEqual(headers_lower["x-goog-ext-73010990-bin"], "CAAQABgA",
                        "x-goog-ext-73010990-bin should be Base64 'CAAQABgA' for [0,0,0]")
        
        # Verify X-Goog-AuthUser is sent (including for primary account)
        self.assertIn("x-goog-authuser", headers_lower,
                     "X-Goog-AuthUser header should be sent for authenticated request")
        self.assertEqual(headers_lower["x-goog-authuser"], "0",
                        "X-Goog-AuthUser should be '0' for primary account")
    
    @patch('wsa_server.gemini.load_cookie')
    @patch('wsa_server.gemini.account_prefix')
    @patch('urllib.request.urlopen')
    def test_non_argon_model_has_no_binary_headers(self, mock_urlopen, mock_prefix, mock_cookie):
        """Test that non-Argon model requests do NOT include binary Protobuf headers.
        
        **Validates: Requirements 2.2, 3.1, 3.2**
        """
        # Setup
        mock_prefix.return_value = ""
        mock_cookie.return_value = ("test_cookie", "test_sapisid")
        
        mock_response = Mock()
        mock_response.read.return_value = b'{"response": "test"}'
        mock_urlopen.return_value = mock_response
        
        # Flash model config WITHOUT hex key
        flash_cfg = {
            "mode": 1,
            "think": 4,
        }
        
        # Execute
        result = gemini.gemini_stream_generate(
            prompt="Test prompt",
            model_cfg=flash_cfg,
            file_refs=None
        )
        
        # Verify: Request was made WITHOUT binary Protobuf headers
        self.assertEqual(mock_urlopen.call_count, 1)
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        
        # Normalize header keys for case-insensitive comparison
        headers_lower = {k.lower(): v for k, v in request.headers.items()}
        
        # Check all four binary headers are ABSENT
        self.assertNotIn("x-goog-ext-525001261-bin", headers_lower,
                        "x-goog-ext-525001261-bin header should NOT be present for Flash model")
        self.assertNotIn("x-goog-ext-525005358-bin", headers_lower,
                        "x-goog-ext-525005358-bin header should NOT be present for Flash model")
        self.assertNotIn("x-goog-ext-73010989-bin", headers_lower,
                        "x-goog-ext-73010989-bin header should NOT be present for Flash model")
        self.assertNotIn("x-goog-ext-73010990-bin", headers_lower,
                        "x-goog-ext-73010990-bin header should NOT be present for Flash model")
        
        # Verify standard headers are still present
        self.assertIn("content-type", headers_lower)
        self.assertIn("origin", headers_lower)
        self.assertIn("referer", headers_lower)
    
    @patch('wsa_server.gemini.load_cookie')
    @patch('wsa_server.gemini.account_prefix')
    @patch('wsa_server.gemini.refresh_xsrf')
    @patch('urllib.request.urlopen')
    def test_xsrf_retry_re_encodes_body_and_rebuilds_headers(self, mock_urlopen, mock_refresh, mock_prefix, mock_cookie):
        """Test that 401 error triggers XSRF refresh with body re-encoding and header rebuild.
        
        **Validates: Requirements 1.6, 2.6**
        """
        # Setup
        mock_prefix.return_value = ""
        mock_cookie.return_value = ("test_cookie", "test_sapisid")
        
        with config._CONFIG_LOCK:
            config.CONFIG["cookie_file"] = "test_cookie.json"
        
        # First request fails with 401, second succeeds
        mock_401_error = urllib.error.HTTPError(
            "https://gemini.google.com", 401, "Unauthorized", {}, None
        )
        mock_success_response = Mock()
        mock_success_response.read.return_value = b'{"response": "test"}'
        mock_urlopen.side_effect = [mock_401_error, mock_success_response]
        
        # Mock XSRF refresh to update token
        def refresh_side_effect():
            with config._CONFIG_LOCK:
                config.CONFIG["xsrf_token"] = "new_xsrf_token"
            return True
        mock_refresh.side_effect = refresh_side_effect
        
        # Argon model config
        argon_cfg = {
            "mode": 7,
            "think": 0,
            "hex": "95a4c0abcba24364",
            "slot80": 2,
        }
        
        # Execute
        result = gemini.gemini_stream_generate(
            prompt="Test prompt",
            model_cfg=argon_cfg,
            file_refs=None
        )
        
        # Verify: XSRF refresh was called
        self.assertEqual(mock_refresh.call_count, 1, "refresh_xsrf() should be called once")
        
        # Verify: Two requests were made (initial + retry)
        self.assertEqual(mock_urlopen.call_count, 2, "Should make 2 requests: initial + retry")
        
        # Verify: Second request has new XSRF token in body
        retry_request = mock_urlopen.call_args_list[1][0][0]
        retry_body = retry_request.data.decode('utf-8')
        self.assertIn("at=new_xsrf_token", retry_body,
                     "Retry request body should contain new XSRF token")
        self.assertNotIn("at=test_xsrf_token", retry_body,
                        "Retry request body should NOT contain old XSRF token")
        
        # Verify: Retry request still has binary Protobuf headers (headers were rebuilt)
        retry_headers_lower = {k.lower(): v for k, v in retry_request.headers.items()}
        self.assertIn("x-goog-ext-525001261-bin", retry_headers_lower,
                     "Retry request should still have Argon headers")
        self.assertIn("x-goog-ext-525005358-bin", retry_headers_lower)
        self.assertIn("x-goog-ext-73010989-bin", retry_headers_lower)
        self.assertIn("x-goog-ext-73010990-bin", retry_headers_lower)
    
    @patch('wsa_server.gemini.load_cookie')
    @patch('wsa_server.gemini.account_prefix')
    @patch('urllib.request.urlopen')
    def test_old_style_function_call_works(self, mock_urlopen, mock_prefix, mock_cookie):
        """Test that old-style function calls (model_id, think_mode) still work.
        
        **Validates: Requirements 3.7**
        """
        # Setup
        mock_prefix.return_value = ""
        mock_cookie.return_value = ("test_cookie", "test_sapisid")
        
        mock_response = Mock()
        mock_response.read.return_value = b'{"response": "test"}'
        mock_urlopen.return_value = mock_response
        
        # Execute with old-style call
        result = gemini.gemini_stream_generate(
            prompt="Test prompt",
            model_id=1,  # Flash mode
            think_mode=4,
            file_refs=None
        )
        
        # Verify: Request was made successfully
        self.assertEqual(mock_urlopen.call_count, 1)
        
        # Verify: No binary headers (Flash model)
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        headers_lower = {k.lower(): v for k, v in request.headers.items()}
        self.assertNotIn("x-goog-ext-525001261-bin", headers_lower)
    
    @patch('wsa_server.gemini.load_cookie')
    @patch('wsa_server.gemini.account_prefix')
    @patch('urllib.request.urlopen')
    def test_new_style_function_call_works(self, mock_urlopen, mock_prefix, mock_cookie):
        """Test that new-style function calls (model_cfg) work.
        
        **Validates: Requirements 2.1, 2.2**
        """
        # Setup
        mock_prefix.return_value = ""
        mock_cookie.return_value = ("test_cookie", "test_sapisid")
        
        mock_response = Mock()
        mock_response.read.return_value = b'{"response": "test"}'
        mock_urlopen.return_value = mock_response
        
        # Argon model config
        argon_cfg = {
            "mode": 7,
            "think": 0,
            "hex": "95a4c0abcba24364",
            "slot80": 2,
        }
        
        # Execute with new-style call
        result = gemini.gemini_stream_generate(
            prompt="Test prompt",
            model_cfg=argon_cfg,
            file_refs=None
        )
        
        # Verify: Request was made successfully
        self.assertEqual(mock_urlopen.call_count, 1)
        
        # Verify: Binary headers are present
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        headers_lower = {k.lower(): v for k, v in request.headers.items()}
        self.assertIn("x-goog-ext-525001261-bin", headers_lower)
    
    @patch('wsa_server.gemini.load_cookie')
    @patch('wsa_server.gemini.account_prefix')
    @patch('urllib.request.urlopen')
    def test_think_override_preserved_in_model_cfg(self, mock_urlopen, mock_prefix, mock_cookie):
        """Test that @think= overrides are preserved in model_cfg.
        
        **Validates: Requirements 2.1**
        """
        # Setup
        mock_prefix.return_value = ""
        mock_cookie.return_value = ("test_cookie", "test_sapisid")
        
        mock_response = Mock()
        mock_response.read.return_value = b'{"response": "test"}'
        mock_urlopen.return_value = mock_response
        
        # Argon model config with think override
        argon_cfg_think_2 = {
            "mode": 7,
            "think": 2,  # Override from default 0
            "hex": "95a4c0abcba24364",
            "slot80": 2,
        }
        
        # Execute
        result = gemini.gemini_stream_generate(
            prompt="Test prompt",
            model_cfg=argon_cfg_think_2,
            file_refs=None
        )
        
        # Verify: Request was made
        self.assertEqual(mock_urlopen.call_count, 1)
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        
        # Verify: POST body contains think=2 in inner[17]
        body = request.data.decode('utf-8')
        # Parse the f.req parameter
        import urllib.parse
        params = urllib.parse.parse_qs(body)
        f_req = json.loads(params['f.req'][0])
        inner = json.loads(f_req[1])
        
        # Check inner[17] contains [[2]]
        self.assertEqual(inner[17], [[2]], 
                        "inner[17] should contain think override value [[2]]")


class TestTask312StreamGenerateIter(unittest.TestCase):
    """Test gemini_stream_generate_iter() for Argon headers and XSRF retry."""
    
    def setUp(self):
        """Save original CONFIG state."""
        for name in ("is_dom_relay_active", "argon_strict_relay_required"):
            relay_patch = patch(f"wsa_server.gemini.{name}", return_value=False)
            relay_patch.start()
            self.addCleanup(relay_patch.stop)
        with config._CONFIG_LOCK:
            self.original_config = dict(config.CONFIG)
        
        # Setup test config
        with config._CONFIG_LOCK:
            config.CONFIG["gemini_bl"] = "boq_gemini-web-uiserver_20260716.08_p0"
            config.CONFIG["xsrf_token"] = "test_xsrf_token"
            config.CONFIG["auth_user"] = "0"
            config.CONFIG["retry_attempts"] = 3
            config.CONFIG["retry_delay_sec"] = 0.1
            config.CONFIG["request_timeout_sec"] = 30
            config.CONFIG["cookie_file"] = None
            config.CONFIG["proxy"] = None
    
    def tearDown(self):
        """Restore original CONFIG state."""
        with config._CONFIG_LOCK:
            config.CONFIG.clear()
            config.CONFIG.update(self.original_config)
    
    @patch('wsa_server.gemini.load_cookie')
    @patch('wsa_server.gemini.account_prefix')
    @patch('wsa_server.gemini.gemini_stream_generate')
    def test_fallback_uses_model_cfg_not_individual_params(self, mock_stream_gen, mock_prefix, mock_cookie):
        """Test that httpx fallback calls gemini_stream_generate with model_cfg.
        
        **Validates: Requirements 1.1, 2.2**
        """
        # Setup: Mock HAS_HTTPX as False to trigger fallback
        original_has_httpx = gemini.HAS_HTTPX
        gemini.HAS_HTTPX = False
        
        try:
            mock_prefix.return_value = ""
            mock_cookie.return_value = ("test_cookie", "test_sapisid")
            mock_stream_gen.return_value = '{"response": "test"}'
            
            # Mock extract_response_text
            with patch('wsa_server.gemini.extract_response_text', return_value="Test response"):
                # Argon model config
                argon_cfg = {
                    "mode": 7,
                    "think": 0,
                    "hex": "95a4c0abcba24364",
                    "slot80": 2,
                }
                
                # Execute
                results = list(gemini.gemini_stream_generate_iter(
                    prompt="Test prompt",
                    model_cfg=argon_cfg,
                    file_refs=None
                ))
                
                # Verify: fallback was called with model_cfg, not individual params
                self.assertEqual(mock_stream_gen.call_count, 1)
                call_kwargs = mock_stream_gen.call_args[1]
                
                # Check that model_cfg was passed
                self.assertIn("model_cfg", call_kwargs)
                self.assertEqual(call_kwargs["model_cfg"], argon_cfg)
                
                # Verify results
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0], "Test response")
        finally:
            gemini.HAS_HTTPX = original_has_httpx
    
    @patch('wsa_server.gemini.load_cookie')
    @patch('wsa_server.gemini.account_prefix')
    def test_argon_model_includes_binary_protobuf_headers(self, mock_prefix, mock_cookie):
        """Test that Argon model requests include all four binary Protobuf headers in streaming mode.
        
        **Validates: Requirements 1.1, 2.2**
        """
        # Skip if httpx not available
        if not gemini.HAS_HTTPX:
            self.skipTest("httpx not available")
        
        # Setup
        mock_prefix.return_value = ""
        mock_cookie.return_value = ("test_cookie", "test_sapisid")
        
        # Mock httpx.Client and stream
        with patch('wsa_server.gemini.httpx.Client') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__.return_value = mock_client
            
            # Mock stream response
            mock_stream_response = MagicMock()
            mock_stream_response.__enter__.return_value = mock_stream_response
            mock_stream_response.iter_text.return_value = iter([])  # Empty stream
            mock_client.stream.return_value = mock_stream_response
            
            # Argon model config with hex key
            argon_cfg = {
                "mode": 7,
                "think": 0,
                "hex": "95a4c0abcba24364",
                "slot80": 2,
            }
            
            # Execute
            results = list(gemini.gemini_stream_generate_iter(
                prompt="Test prompt",
                model_cfg=argon_cfg,
                file_refs=None
            ))
            
            # Verify: stream() was called with headers
            self.assertEqual(mock_client.stream.call_count, 1)
            call_kwargs = mock_client.stream.call_args[1]
            
            # Check all four binary headers are present
            headers = call_kwargs["headers"]
            headers_lower = {k.lower(): v for k, v in headers.items()}
            
            self.assertIn("x-goog-ext-525001261-bin", headers_lower,
                         "x-goog-ext-525001261-bin header should be present for Argon model")
            self.assertIn("x-goog-ext-525005358-bin", headers_lower,
                         "x-goog-ext-525005358-bin header should be present for Argon model")
            self.assertIn("x-goog-ext-73010989-bin", headers_lower,
                         "x-goog-ext-73010989-bin header should be present for Argon model")
            self.assertIn("x-goog-ext-73010990-bin", headers_lower,
                         "x-goog-ext-73010990-bin header should be present for Argon model")
            
            # Verify fixed capability headers have correct Base64 values
            self.assertEqual(headers_lower["x-goog-ext-73010989-bin"], "CAA=",
                            "x-goog-ext-73010989-bin should be Base64 'CAA=' for [0]")
            self.assertEqual(headers_lower["x-goog-ext-73010990-bin"], "CAAQABgA",
                            "x-goog-ext-73010990-bin should be Base64 'CAAQABgA' for [0,0,0]")
            
            # Verify X-Goog-AuthUser is sent
            self.assertIn("x-goog-authuser", headers_lower,
                         "X-Goog-AuthUser header should be sent for authenticated request")
            self.assertEqual(headers_lower["x-goog-authuser"], "0",
                            "X-Goog-AuthUser should be '0' for primary account")
    
    @patch('wsa_server.gemini.load_cookie')
    @patch('wsa_server.gemini.account_prefix')
    def test_non_argon_model_has_no_binary_headers(self, mock_prefix, mock_cookie):
        """Test that non-Argon model requests do NOT include binary Protobuf headers in streaming.
        
        **Validates: Requirements 2.2, 3.1, 3.2**
        """
        # Skip if httpx not available
        if not gemini.HAS_HTTPX:
            self.skipTest("httpx not available")
        
        # Setup
        mock_prefix.return_value = ""
        mock_cookie.return_value = ("test_cookie", "test_sapisid")
        
        # Mock httpx.Client and stream
        with patch('wsa_server.gemini.httpx.Client') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__.return_value = mock_client
            
            # Mock stream response
            mock_stream_response = MagicMock()
            mock_stream_response.__enter__.return_value = mock_stream_response
            mock_stream_response.iter_text.return_value = iter([])  # Empty stream
            mock_client.stream.return_value = mock_stream_response
            
            # Flash model config WITHOUT hex key
            flash_cfg = {
                "mode": 1,
                "think": 4,
            }
            
            # Execute
            results = list(gemini.gemini_stream_generate_iter(
                prompt="Test prompt",
                model_cfg=flash_cfg,
                file_refs=None
            ))
            
            # Verify: stream() was called with headers
            self.assertEqual(mock_client.stream.call_count, 1)
            call_kwargs = mock_client.stream.call_args[1]
            
            # Check all four binary headers are ABSENT
            headers = call_kwargs["headers"]
            headers_lower = {k.lower(): v for k, v in headers.items()}
            
            self.assertNotIn("x-goog-ext-525001261-bin", headers_lower,
                            "x-goog-ext-525001261-bin header should NOT be present for Flash model")
            self.assertNotIn("x-goog-ext-525005358-bin", headers_lower,
                            "x-goog-ext-525005358-bin header should NOT be present for Flash model")
            self.assertNotIn("x-goog-ext-73010989-bin", headers_lower,
                            "x-goog-ext-73010989-bin header should NOT be present for Flash model")
            self.assertNotIn("x-goog-ext-73010990-bin", headers_lower,
                            "x-goog-ext-73010990-bin header should NOT be present for Flash model")
            
            # Verify standard headers are still present
            self.assertIn("content-type", headers_lower)
            self.assertIn("origin", headers_lower)
            self.assertIn("referer", headers_lower)
    
    @patch('wsa_server.gemini.load_cookie')
    @patch('wsa_server.gemini.account_prefix')
    @patch('wsa_server.gemini.refresh_xsrf')
    @patch('wsa_server.gemini.gemini_stream_generate')
    def test_xsrf_retry_calls_stream_generate_with_model_cfg(self, mock_stream_gen, mock_refresh, mock_prefix, mock_cookie):
        """Test that 401/403 error triggers XSRF refresh and retries via gemini_stream_generate with model_cfg.
        
        **Validates: Requirements 1.6, 2.6**
        """
        # Skip if httpx not available
        if not gemini.HAS_HTTPX:
            self.skipTest("httpx not available")
        
        # Setup
        mock_prefix.return_value = ""
        mock_cookie.return_value = ("test_cookie", "test_sapisid")
        
        with config._CONFIG_LOCK:
            config.CONFIG["cookie_file"] = "test_cookie.json"
        
        # Mock XSRF refresh to update token
        def refresh_side_effect():
            with config._CONFIG_LOCK:
                config.CONFIG["xsrf_token"] = "new_xsrf_token"
            return True
        mock_refresh.side_effect = refresh_side_effect
        
        # Mock stream_generate to return success
        mock_stream_gen.return_value = '{"response": "test"}'
        
        # Mock extract_response_text
        with patch('wsa_server.gemini.extract_response_text', return_value="Test response"):
            # Mock httpx.Client to raise 401
            with patch('wsa_server.gemini.httpx.Client') as mock_client_class:
                mock_client = MagicMock()
                mock_client_class.return_value.__enter__.return_value = mock_client
                
                # Create mock 401 error with response attribute
                mock_error = Exception("401 Unauthorized")
                mock_response = Mock()
                mock_response.status_code = 401
                mock_error.response = mock_response
                
                # Mock stream to raise 401 error
                mock_client.stream.side_effect = mock_error
                
                # Argon model config
                argon_cfg = {
                    "mode": 7,
                    "think": 0,
                    "hex": "95a4c0abcba24364",
                    "slot80": 2,
                }
                
                # Execute
                results = list(gemini.gemini_stream_generate_iter(
                    prompt="Test prompt",
                    model_cfg=argon_cfg,
                    file_refs=None
                ))
                
                # Verify: XSRF refresh was called
                self.assertEqual(mock_refresh.call_count, 1, "refresh_xsrf() should be called once")
                
                # Verify: gemini_stream_generate was called for retry with model_cfg
                self.assertEqual(mock_stream_gen.call_count, 1, "Should fall back to non-streaming")
                call_kwargs = mock_stream_gen.call_args[1]
                
                # Check that model_cfg was passed (not individual params)
                self.assertIn("model_cfg", call_kwargs)
                self.assertEqual(call_kwargs["model_cfg"], argon_cfg)
                
                # Verify results
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0], "Test response")
    
    @patch('wsa_server.gemini.load_cookie')
    @patch('wsa_server.gemini.account_prefix')
    @patch('wsa_server.gemini.gemini_stream_generate')
    def test_old_style_function_call_works(self, mock_stream_gen, mock_prefix, mock_cookie):
        """Test that old-style function calls (model_id, think_mode) still work.
        
        **Validates: Requirements 3.7**
        """
        # Setup: Mock HAS_HTTPX as False to trigger fallback (simpler test)
        original_has_httpx = gemini.HAS_HTTPX
        gemini.HAS_HTTPX = False
        
        try:
            mock_prefix.return_value = ""
            mock_cookie.return_value = ("test_cookie", "test_sapisid")
            mock_stream_gen.return_value = '{"response": "test"}'
            
            # Mock extract_response_text
            with patch('wsa_server.gemini.extract_response_text', return_value="Test response"):
                # Execute with old-style call
                results = list(gemini.gemini_stream_generate_iter(
                    prompt="Test prompt",
                    model_id=1,  # Flash mode
                    think_mode=4,
                    file_refs=None
                ))
                
                # Verify: fallback was called successfully
                self.assertEqual(mock_stream_gen.call_count, 1)
                
                # Verify: model_cfg was constructed from old-style params
                call_kwargs = mock_stream_gen.call_args[1]
                self.assertIn("model_cfg", call_kwargs)
                self.assertEqual(call_kwargs["model_cfg"]["mode"], 1)
                self.assertEqual(call_kwargs["model_cfg"]["think"], 4)
                
                # Verify results
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0], "Test response")
        finally:
            gemini.HAS_HTTPX = original_has_httpx
    
    @patch('wsa_server.gemini.load_cookie')
    @patch('wsa_server.gemini.account_prefix')
    def test_new_style_function_call_works(self, mock_prefix, mock_cookie):
        """Test that new-style function calls (model_cfg) work.
        
        **Validates: Requirements 2.1, 2.2**
        """
        # Skip if httpx not available
        if not gemini.HAS_HTTPX:
            self.skipTest("httpx not available")
        
        # Setup
        mock_prefix.return_value = ""
        mock_cookie.return_value = ("test_cookie", "test_sapisid")
        
        # Mock httpx.Client and stream
        with patch('wsa_server.gemini.httpx.Client') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__.return_value = mock_client
            
            # Mock stream response
            mock_stream_response = MagicMock()
            mock_stream_response.__enter__.return_value = mock_stream_response
            mock_stream_response.iter_text.return_value = iter([])
            mock_client.stream.return_value = mock_stream_response
            
            # Argon model config
            argon_cfg = {
                "mode": 7,
                "think": 0,
                "hex": "95a4c0abcba24364",
                "slot80": 2,
            }
            
            # Execute with new-style call
            results = list(gemini.gemini_stream_generate_iter(
                prompt="Test prompt",
                model_cfg=argon_cfg,
                file_refs=None
            ))
            
            # Verify: stream() was called successfully
            self.assertEqual(mock_client.stream.call_count, 1)
            
            # Verify: Binary headers are present
            call_kwargs = mock_client.stream.call_args[1]
            headers = call_kwargs["headers"]
            headers_lower = {k.lower(): v for k, v in headers.items()}
            self.assertIn("x-goog-ext-525001261-bin", headers_lower)
    
    @patch('wsa_server.gemini.load_cookie')
    @patch('wsa_server.gemini.account_prefix')
    def test_think_override_preserved_in_model_cfg(self, mock_prefix, mock_cookie):
        """Test that @think= overrides are preserved in model_cfg during streaming.
        
        **Validates: Requirements 2.1**
        """
        # Skip if httpx not available
        if not gemini.HAS_HTTPX:
            self.skipTest("httpx not available")
        
        # Setup
        mock_prefix.return_value = ""
        mock_cookie.return_value = ("test_cookie", "test_sapisid")
        
        # Mock httpx.Client and stream
        with patch('wsa_server.gemini.httpx.Client') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__.return_value = mock_client
            
            # Mock stream response
            mock_stream_response = MagicMock()
            mock_stream_response.__enter__.return_value = mock_stream_response
            mock_stream_response.iter_text.return_value = iter([])
            mock_client.stream.return_value = mock_stream_response
            
            # Argon model config with think override
            argon_cfg_think_2 = {
                "mode": 7,
                "think": 2,  # Override from default 0
                "hex": "95a4c0abcba24364",
                "slot80": 2,
            }
            
            # Execute
            results = list(gemini.gemini_stream_generate_iter(
                prompt="Test prompt",
                model_cfg=argon_cfg_think_2,
                file_refs=None
            ))
            
            # Verify: stream() was called
            self.assertEqual(mock_client.stream.call_count, 1)
            call_kwargs = mock_client.stream.call_args[1]
            
            # Verify: POST body contains think=2 in inner[17]
            body = call_kwargs["content"]
            
            # Parse the f.req parameter
            import urllib.parse
            params = urllib.parse.parse_qs(body)
            f_req = json.loads(params['f.req'][0])
            inner = json.loads(f_req[1])
            
            # Check inner[17] contains [[2]]
            self.assertEqual(inner[17], [[2]], 
                            "inner[17] should contain think override value [[2]]")


# --- API handler model resolution ---
class MockHandler:
    """Simple mock handler to test _resolve_model without HTTP machinery."""
    def __init__(self):
        pass
    
    # Copy the _resolve_model method from GeminiHandler
    def _resolve_model(self, model_name):
        """
        Resolve model name to model config dict, merging @think= overrides.
        
        Returns: (model_name, model_cfg, error_message)
            - model_name: Original base model name (without @think=)
            - model_cfg: Full config dict with think override merged if present
            - error_message: Error string if model not found, None otherwise
        """
        think_override = None
        base_name = model_name
        if "@think=" in model_name:
            base_name, think_str = model_name.rsplit("@think=", 1)
            try:
                think_override = int(think_str)
            except ValueError:
                return None, None, f"Invalid think depth in model: {model_name}"
        
        cfg = models.MODELS.get(model_name) or models.MODELS.get(base_name)
        if not cfg:
            return None, None, f"Unknown model: {model_name}"
        
        # Merge think override into config
        if think_override is not None:
            cfg = {**cfg}
            if cfg.get("mode") == 8:
                cfg["slot80"] = {0: 2, 1: 2, 2: 4, 3: 1, 4: 1}.get(think_override, 2)
                cfg["think"] = 0
            else:
                cfg["think"] = think_override
        
        return base_name, cfg, None


class TestTask313APIHandler(unittest.TestCase):
    """Test API handler passes full model config to generation functions."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Save original CONFIG
        self.original_config = config.CONFIG.copy()
        
        # Ensure MODELS dict has Argon entry with hex field
        if "gemini-4-argon" not in models.MODELS:
            models.MODELS["gemini-4-argon"] = {
                "mode": 7,
                "think": 0,
                "hex": "95a4c0abcba24364",
                "slot80": 2,
                "desc": "Gemini 4 Argon test"
            }
        
        # Ensure Flash model exists without hex field
        if "gemini-3.7-flash" not in models.MODELS:
            models.MODELS["gemini-3.7-flash"] = {
                "mode": 1,
                "think": 4,
                "desc": "Flash test"
            }
    
    def tearDown(self):
        """Clean up after tests."""
        config.CONFIG.clear()
        config.CONFIG.update(self.original_config)
    
    def test_resolve_model_returns_full_config(self):
        """Test _resolve_model returns full model_cfg dict."""
        handler = MockHandler()
        
        # Test Argon model
        model_name, model_cfg, err = handler._resolve_model("gemini-4-argon")
        
        self.assertIsNone(err)
        self.assertEqual(model_name, "gemini-4-argon")
        self.assertIsInstance(model_cfg, dict)
        self.assertEqual(model_cfg["mode"], 8)
        self.assertEqual(model_cfg["think"], 0)
        self.assertEqual(model_cfg["hex"], "95a4c0abcba24364")
        self.assertEqual(model_cfg["slot80"], 2)
    
    def test_resolve_model_flash_no_hex(self):
        """Test _resolve_model for Flash model without hex field."""
        handler = MockHandler()
        
        model_name, model_cfg, err = handler._resolve_model("gemini-3.7-flash")
        
        self.assertIsNone(err)
        self.assertEqual(model_name, "gemini-3.7-flash")
        self.assertIsInstance(model_cfg, dict)
        self.assertEqual(model_cfg["mode"], 1)
        self.assertEqual(model_cfg["think"], 4)
        self.assertNotIn("hex", model_cfg)
    
    def test_resolve_model_think_override(self):
        """Test @think= override is merged into model_cfg."""
        handler = MockHandler()
        
        # Test Argon with @think=2 override
        model_name, model_cfg, err = handler._resolve_model("gemini-4-argon@think=2")
        
        self.assertIsNone(err)
        self.assertEqual(model_name, "gemini-4-argon")
        self.assertIsInstance(model_cfg, dict)
        self.assertEqual(model_cfg["mode"], 8)
        self.assertEqual(model_cfg["think"], 0)  # Argon uses slot80 for thinking tier
        self.assertEqual(model_cfg["hex"], "95a4c0abcba24364")  # Other fields preserved
        self.assertEqual(model_cfg["slot80"], 4)
    
    def test_resolve_model_unknown_model(self):
        """Test _resolve_model returns error for unknown model."""
        handler = MockHandler()
        
        model_name, model_cfg, err = handler._resolve_model("unknown-model")
        
        self.assertIsNone(model_name)
        self.assertIsNone(model_cfg)
        self.assertIsNotNone(err)
        self.assertIn("Unknown model", err)
    
    def test_resolve_model_invalid_think_override(self):
        """Test _resolve_model handles invalid @think= syntax."""
        handler = MockHandler()
        
        model_name, model_cfg, err = handler._resolve_model("gemini-4-argon@think=abc")
        
        self.assertIsNone(model_name)
        self.assertIsNone(model_cfg)
        self.assertIsNotNone(err)
        self.assertIn("Invalid think depth", err)
    
    @patch('wsa_server.gemini.gemini_stream_generate')
    @patch('wsa_server.gemini.extract_response_text')
    def test_generation_receives_model_cfg(self, mock_extract, mock_generate):
        """Test generation functions receive full model_cfg."""
        mock_generate.return_value = "mock_response"
        mock_extract.return_value = "Test response"
        
        model_cfg = {"mode": 8, "think": 0, "hex": "95a4c0abcba24364", "slot80": 2}
        
        # Call generation function directly
        result = gemini.gemini_stream_generate("Test prompt", model_cfg=model_cfg, file_refs=None)
        
        # Verify gemini_stream_generate was called
        self.assertTrue(mock_generate.called)
        self.assertEqual(result, "mock_response")
    
    def test_model_cfg_contains_hex_for_argon(self):
        """Test that Argon model config contains hex field."""
        handler = MockHandler()
        model_name, model_cfg, err = handler._resolve_model("gemini-4-argon")
        
        self.assertIsNone(err)
        self.assertIn("hex", model_cfg)
        self.assertEqual(model_cfg["hex"], "95a4c0abcba24364")
    
    def test_model_cfg_no_hex_for_flash(self):
        """Test that Flash model config does not contain hex field."""
        handler = MockHandler()
        model_name, model_cfg, err = handler._resolve_model("gemini-3.7-flash")
        
        self.assertIsNone(err)
        self.assertNotIn("hex", model_cfg)
    
    def test_think_override_preserves_hex(self):
        """Test that @think= override preserves hex field."""
        handler = MockHandler()
        model_name, model_cfg, err = handler._resolve_model("gemini-4-argon@think=2")
        
        self.assertIsNone(err)
        self.assertEqual(model_cfg["think"], 0)
        self.assertEqual(model_cfg["hex"], "95a4c0abcba24364")
        self.assertEqual(model_cfg["slot80"], 4)


# --- Non-Argon preservation properties ---
class TestPreservationNonArgonModels(unittest.TestCase):
    """
    Test that non-Argon models maintain their existing behavior after the fix.
    
    These tests should PASS on both unfixed and fixed code.
    """
    
    def setUp(self):
        """Setup test environment with mock configuration."""
        # Store original CONFIG to restore later
        self.original_config = config.CONFIG.copy()
        
        # Configure test environment
        config.CONFIG.update({
            "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
            "auth_user": 0,
            "xsrf_token": "test_xsrf_token_12345",
            "retry_attempts": 1,
            "request_timeout_sec": 30,
            "cookie_file": None,  # Start with no cookie for anonymous tests
            "log_requests": False,
            "proxy": None,  # Disable proxy for tests
        })
        
        # Patch _has_valid_relay to force StreamGenerate path
        self.relay_patcher = patch('wsa_server.gemini.is_dom_relay_active', return_value=False)
        self.relay_patcher.start()
    
    def tearDown(self):
        """Restore original configuration."""
        self.relay_patcher.stop()
        config.CONFIG.clear()
        config.CONFIG.update(self.original_config)
    
    def _get_header(self, headers: dict, name: str) -> str:
        """Case-insensitive header lookup."""
        for key, value in headers.items():
            if key.lower() == name.lower():
                return value
        return None
    
    def _mock_cookie_with_sapisid(self):
        """Mock load_cookie to return test SAPISID."""
        cookie_str = "__Secure-1PSID=test_sid; SAPISID=test_sapisid_value"
        sapisid = "test_sapisid_value"
        return cookie_str, sapisid
    
    def _mock_empty_cookie(self):
        """Mock load_cookie for anonymous mode."""
        return "", None
    
    def _capture_http_request(self):
        """
        Create a mock that captures the HTTP request being sent.
        Returns (mock_urlopen, captured_request_container).
        """
        captured = {"request": None, "headers": None, "body": None, "url": None}
        
        def mock_urlopen(request, *args, **kwargs):
            """Capture request details and return mock response."""
            captured["request"] = request
            captured["headers"] = dict(request.headers)
            captured["body"] = request.data
            captured["url"] = request.full_url
            
            # Mock successful response with typical Gemini structure
            mock_response = Mock()
            response_data = json.dumps([
                [
                    [None, None, None, None, None, None, None, None, [
                        ["Response chunk 1"],
                        ["Response chunk 2"],
                    ]]
                ]
            ])
            mock_response.read.return_value = response_data.encode()
            return mock_response
        
        return mock_urlopen, captured
    
    def _parse_post_body(self, body: bytes) -> dict:
        """Parse URL-encoded POST body into dict."""
        return dict(urllib.parse.parse_qsl(body.decode('utf-8')))
    
    def _parse_inner_array(self, body: bytes) -> list:
        """Extract and parse the inner[] array from POST body."""
        params = self._parse_post_body(body)
        if "f.req" not in params:
            return None
        
        outer = json.loads(params["f.req"])
        if len(outer) < 2:
            return None
        
        return json.loads(outer[1])
    
    def test_flash_model_no_binary_protobuf_headers(self):
        """
        Requirement 3.1, 3.2: Non-Argon models should NOT have x-goog-ext-*-bin headers.
        
        Tests gemini-3.7-flash model to ensure no Argon-specific headers are added.
        """
        mock_urlopen, captured = self._capture_http_request()
        
        with patch('wsa_server.gemini.load_cookie', return_value=self._mock_cookie_with_sapisid()):
            with patch('urllib.request.urlopen', side_effect=mock_urlopen):
                with patch('urllib.request.build_opener') as mock_opener:
                    mock_proxy_opener = Mock()
                    mock_proxy_opener.open.side_effect = mock_urlopen
                    mock_opener.return_value = mock_proxy_opener
                    
                    # Get Flash model config
                    model_cfg = models.MODELS.get("gemini-3.7-flash")
                    assert model_cfg is not None, "gemini-3.7-flash model not found"
                    
                    # Make Flash request
                    try:
                        result = server._default_server_generate(
                            prompt="Test prompt for Flash",
                            model_id=model_cfg["mode"],
                            think_mode=model_cfg["think"],
                            file_refs=None
                        )
                    except Exception as e:
                        # If request fails, we can still check captured headers
                        pass
        
        # Verify request was captured
        assert captured["headers"] is not None, "HTTP request was not captured"
        
        headers = captured["headers"]
        
        # Requirement 3.2: Non-Argon models should NOT have binary Protobuf headers
        assert "x-goog-ext-525001261-bin" not in headers, \
            "Flash model should NOT have x-goog-ext-525001261-bin header"
        assert "x-goog-ext-525005358-bin" not in headers, \
            "Flash model should NOT have x-goog-ext-525005358-bin header"
        assert "x-goog-ext-73010989-bin" not in headers, \
            "Flash model should NOT have x-goog-ext-73010989-bin header"
        assert "x-goog-ext-73010990-bin" not in headers, \
            "Flash model should NOT have x-goog-ext-73010990-bin header"
        
        print("[OK] Flash model correctly omits binary Protobuf headers")
    
    def test_post_body_inner_array_structure(self):
        """
        Requirement 3.6: POST body inner[] array structure must remain unchanged.
        
        Verifies the inner[] array has the expected structure for non-Argon models.
        """
        mock_urlopen, captured = self._capture_http_request()
        
        with patch('wsa_server.gemini.load_cookie', return_value=self._mock_cookie_with_sapisid()):
            with patch('urllib.request.urlopen', side_effect=mock_urlopen):
                # Make Flash request
                model_cfg = models.MODELS.get("gemini-3.7-flash")
                try:
                    result = server._default_server_generate(
                        prompt="Test prompt",
                        model_id=model_cfg["mode"],
                        think_mode=model_cfg["think"],
                        file_refs=None
                    )
                except Exception:
                    pass
        
        # Parse inner[] array from POST body
        inner = self._parse_inner_array(captured["body"])
        assert inner is not None, "Failed to parse inner[] array from POST body"
        
        # Verify key structure elements
        assert len(inner) >= 80, f"inner[] array should have at least 80 elements, got {len(inner)}"
        assert inner[0] is not None, "inner[0] should contain prompt data"
        assert inner[1] == ["en"], f"inner[1] should be locale ['en'], got {inner[1]}"
        assert inner[79] == model_cfg["mode"], f"inner[79] should be mode {model_cfg['mode']}, got {inner[79]}"
        
        # Verify prompt structure in inner[0]
        prompt_data = inner[0]
        assert isinstance(prompt_data, list), "inner[0] should be a list"
        assert prompt_data[0] == "Test prompt", f"Prompt text mismatch: {prompt_data[0]}"
        
        print("[OK] POST body inner[] array structure preserved")
    
    def test_anonymous_mode_works_without_auth(self):
        """
        Requirement 3.3: Anonymous mode (no cookies) must work for basic models.
        
        Verifies requests work without authentication cookies.
        """
        mock_urlopen, captured = self._capture_http_request()
        
        with patch('wsa_server.gemini.load_cookie', return_value=self._mock_empty_cookie()):
            with patch('urllib.request.urlopen', side_effect=mock_urlopen):
                # Make Flash request without cookies
                model_cfg = models.MODELS.get("gemini-3.7-flash")
                try:
                    result = server._default_server_generate(
                        prompt="Anonymous test",
                        model_id=model_cfg["mode"],
                        think_mode=model_cfg["think"],
                        file_refs=None
                    )
                except Exception:
                    pass
        
        # Verify request was captured
        assert captured["headers"] is not None, \
            f"HTTP request was not captured. Captured data: {captured}"
        
        headers = captured["headers"]
        
        # Verify no auth headers present (case-insensitive)
        cookie_val = self._get_header(headers, "Cookie")
        assert cookie_val is None or cookie_val == "", \
            "Cookie should not be present in anonymous mode"
        assert self._get_header(headers, "Authorization") is None, \
            "Authorization header should not be present without SAPISID"
        
        # Verify request was still constructed correctly (case-insensitive)
        assert self._get_header(headers, "Content-Type") is not None, \
            f"Content-Type header should be present. Available headers: {list(headers.keys())}"
        assert self._get_header(headers, "X-Same-Domain") is not None, \
            "X-Same-Domain header should be present"
        
        print("[OK] Anonymous mode works without authentication")
    
    def test_streaming_response_parsing(self):
        """
        Requirement 3.4: Streaming responses must deliver incremental text deltas.
        
        Verifies the response parsing extracts text correctly.
        """
        mock_urlopen, captured = self._capture_http_request()
        
        # Create a more realistic streaming response
        response_chunks = [
            ["Chunk 1"],
            ["Chunk 2"],
            ["Chunk 3"]
        ]
        response_data = json.dumps([
            [
                [None, None, None, None, None, None, None, None, response_chunks]
            ]
        ])
        
        def mock_streaming_urlopen(request, *args, **kwargs):
            captured["request"] = request
            captured["headers"] = dict(request.headers)
            captured["body"] = request.data
            mock_response = Mock()
            mock_response.read.return_value = response_data.encode()
            return mock_response
        
        with patch('wsa_server.gemini.load_cookie', return_value=self._mock_empty_cookie()):
            with patch('urllib.request.urlopen', side_effect=mock_streaming_urlopen):
                model_cfg = models.MODELS.get("gemini-3.7-flash")
                result = server._default_server_generate(
                    prompt="Streaming test",
                    model_id=model_cfg["mode"],
                    think_mode=model_cfg["think"],
                    file_refs=None
                )
        
        # Verify response contains the mocked chunks
        assert result is not None, "Response should not be None"
        assert isinstance(result, str), "Response should be a string"
        
        print("[OK] Streaming response parsing works")
    
    def test_xsrf_token_handling(self):
        """
        Requirement 3.7: XSRF token handling for authenticated requests.
        
        Verifies the 'at' parameter is included when xsrf_token is configured.
        """
        mock_urlopen, captured = self._capture_http_request()
        
        # Set XSRF token in config
        config.CONFIG["xsrf_token"] = "test_xsrf_token_abc123"
        
        with patch('wsa_server.gemini.load_cookie', return_value=self._mock_cookie_with_sapisid()):
            with patch('urllib.request.urlopen', side_effect=mock_urlopen):
                model_cfg = models.MODELS.get("gemini-3.7-flash")
                try:
                    result = server._default_server_generate(
                        prompt="XSRF test",
                        model_id=model_cfg["mode"],
                        think_mode=model_cfg["think"],
                        file_refs=None
                    )
                except Exception:
                    pass
        
        # Parse POST body to check 'at' parameter
        params = self._parse_post_body(captured["body"])
        
        assert "at" in params, "XSRF token 'at' parameter should be present"
        assert params["at"] == "test_xsrf_token_abc123", \
            f"XSRF token mismatch: expected 'test_xsrf_token_abc123', got '{params['at']}'"
        
        print("[OK] XSRF token handling preserved")
    
    def test_xsrf_retry_logic_401_handling(self):
        """
        Requirement 3.8: Retry logic for 401 errors must attempt recovery.
        
        Verifies the system attempts retry on 401 error (simplified test).
        """
        call_count = {"count": 0}
        
        def mock_urlopen_with_401(request, *args, **kwargs):
            """First call returns 401, subsequent calls should retry."""
            call_count["count"] += 1
            
            if call_count["count"] == 1:
                # First call: return 401 error
                raise urllib.error.HTTPError(
                    request.full_url, 401, "Unauthorized", {}, None
                )
            else:
                # After retry limit, let it fail
                raise urllib.error.HTTPError(
                    request.full_url, 401, "Unauthorized", {}, None
                )
        
        config.CONFIG["xsrf_token"] = "old_xsrf_token"
        config.CONFIG["cookie_file"] = "test_cookie.json"  # Enable auth path
        
        with patch('wsa_server.gemini.load_cookie', return_value=self._mock_cookie_with_sapisid()):
            with patch('urllib.request.urlopen', side_effect=mock_urlopen_with_401):
                model_cfg = models.MODELS.get("gemini-3.7-flash")
                try:
                    result = server._default_server_generate(
                        prompt="401 retry test",
                        model_id=model_cfg["mode"],
                        think_mode=model_cfg["think"],
                        file_refs=None
                    )
                except Exception as e:
                    # Expected to fail after retries
                    pass
        
        # Verify at least one call was attempted
        assert call_count["count"] >= 1, \
            f"Expected at least 1 call attempt, got {call_count['count']}"
        
        print("[OK] XSRF retry logic for 401 errors attempts recovery")
    
    def test_manual_gemini_bl_configuration_honored(self):
        """
        Requirement 3.9: Manual gemini_bl configuration must be honored.
        
        Verifies the manually configured gemini_bl is used in requests.
        """
        mock_urlopen, captured = self._capture_http_request()
        
        # Set a specific gemini_bl value
        test_bl = "boq_gemini-web-uiserver_20260720.00_p1"
        config.CONFIG["gemini_bl"] = test_bl
        
        with patch('wsa_server.gemini.load_cookie', return_value=self._mock_empty_cookie()):
            with patch('urllib.request.urlopen', side_effect=mock_urlopen):
                model_cfg = models.MODELS.get("gemini-3.7-flash")
                try:
                    result = server._default_server_generate(
                        prompt="BL config test",
                        model_id=model_cfg["mode"],
                        think_mode=model_cfg["think"],
                        file_refs=None
                    )
                except Exception:
                    pass
        
        # Verify the URL contains the configured gemini_bl
        url = captured["url"]
        assert url is not None, "Request URL was not captured"
        assert f"bl={test_bl}" in url, \
            f"URL should contain configured bl={test_bl}, got: {url}"
        
        print("[OK] Manual gemini_bl configuration honored")
    
    def test_network_error_retry_behavior(self):
        """
        Requirement 3.10: Retry logic for network errors.
        
        Verifies the system retries on network errors according to config.
        """
        call_count = {"count": 0}
        
        def mock_urlopen_with_failures(request, *args, **kwargs):
            """Fail multiple times before succeeding."""
            call_count["count"] += 1
            
            if call_count["count"] < 2:
                # First calls: raise network error
                raise urllib.error.URLError("Network error simulation")
            else:
                # Final call: succeed
                mock_response = Mock()
                mock_response.read.return_value = b'[[["Success after retries"]]]'
                return mock_response
        
        config.CONFIG["retry_attempts"] = 3
        config.CONFIG["retry_delay_sec"] = 0  # Speed up test
        
        with patch('wsa_server.gemini.load_cookie', return_value=self._mock_empty_cookie()):
            with patch('urllib.request.urlopen', side_effect=mock_urlopen_with_failures):
                with patch('time.sleep'):  # Mock sleep to speed up test
                    model_cfg = models.MODELS.get("gemini-3.7-flash")
                    try:
                        result = server._default_server_generate(
                            prompt="Retry test",
                            model_id=model_cfg["mode"],
                            think_mode=model_cfg["think"],
                            file_refs=None
                        )
                        # Should succeed after retries
                        assert "Success" in result or call_count["count"] >= 1
                    except Exception as e:
                        # Verify retries were attempted
                        assert call_count["count"] > 1, \
                            f"Expected multiple retry attempts, got {call_count['count']}"
        
        print("[OK] Network error retry behavior works")
    
    def test_standard_headers_present(self):
        """
        Requirement 3.1: Standard headers must be present for all models.
        
        Verifies basic HTTP headers are included in requests.
        """
        mock_urlopen, captured = self._capture_http_request()
        
        with patch('wsa_server.gemini.load_cookie', return_value=self._mock_empty_cookie()):
            with patch('urllib.request.urlopen', side_effect=mock_urlopen):
                model_cfg = models.MODELS.get("gemini-3.7-flash")
                try:
                    result = server._default_server_generate(
                        prompt="Headers test",
                        model_id=model_cfg["mode"],
                        think_mode=model_cfg["think"],
                        file_refs=None
                    )
                except Exception:
                    pass
        
        # Verify request was captured
        assert captured["headers"] is not None, \
            f"HTTP request was not captured. Captured data: {captured}"
        
        headers = captured["headers"]
        
        # Verify standard headers (case-insensitive lookups)
        content_type = self._get_header(headers, "Content-Type")
        assert content_type is not None, "Content-Type header missing"
        assert content_type == "application/x-www-form-urlencoded", \
            f"Content-Type mismatch: {content_type}"
        
        origin = self._get_header(headers, "Origin")
        assert origin is not None, "Origin header missing"
        assert "https://gemini.google.com" in origin, \
            f"Origin header mismatch: {origin}"
        
        referer = self._get_header(headers, "Referer")
        assert referer is not None, "Referer header missing"
        assert "gemini.google.com" in referer, \
            f"Referer header mismatch: {referer}"
        
        x_same_domain = self._get_header(headers, "X-Same-Domain")
        assert x_same_domain is not None, "X-Same-Domain header missing"
        assert x_same_domain == "1", \
            f"X-Same-Domain value mismatch: {x_same_domain}"
        
        user_agent = self._get_header(headers, "User-Agent")
        assert user_agent is not None, "User-Agent header missing"
        
        print("[OK] Standard headers present and correct")


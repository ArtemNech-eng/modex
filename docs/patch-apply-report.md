# Отчёт patch-apply

дата: 2026-08-05T14:08:25Z
коммит: c2506abd9b11ddbe8907811be8de3c0db5e9926d
запуск: 31013649770
код патча: 0
код pytest: 0

## патч
```
== scripts/_patch_floor.py
патч пола по обороту: отвязка от депозита, версия 1
python 3.11.15, cwd /home/runner/work/modex/modex
ПРИМЕНЕНО:
 + объяснение пола по обороту
 + значение пола по умолчанию
 + фраза о депозите в src/api/main.py: замен сделано 1
Скрипт удалил себя.
```

## pytest, последние 12000 символов
```
........................................................................ [ 98%]
.                                                                        [100%]
==================================== PASSES ====================================
=========================== short test summary info ============================
PASSED tests/test_volume_events.py::test_turnover_is_in_rubles_with_lot_size
PASSED tests/test_volume_events.py::test_price_enters_the_turnover
PASSED tests/test_volume_events.py::test_forming_bar_is_ignored
PASSED tests/test_volume_events.py::test_too_little_history
PASSED tests/test_volume_events.py::test_baseline_skips_empty_minutes
PASSED tests/test_volume_events.py::test_baseline_excludes_the_measured_bar
PASSED tests/test_volume_events.py::test_no_baseline_no_events
PASSED tests/test_volume_events.py::test_surge_is_the_case_from_the_request
PASSED tests/test_volume_events.py::test_ordinary_volume_is_not_a_surge
PASSED tests/test_volume_events.py::test_acceleration_matches_the_example
PASSED tests/test_volume_events.py::test_growth_from_nothing_is_not_acceleration
PASSED tests/test_volume_events.py::test_one_spike_is_not_acceleration
PASSED tests/test_volume_events.py::test_growth_must_be_uninterrupted
PASSED tests/test_volume_events.py::test_time_of_day_profile_needs_enough_days
PASSED tests/test_volume_events.py::test_time_of_day_profile_is_used_when_present
PASSED tests/test_volume_events.py::test_source_of_the_baseline_is_always_reported
PASSED tests/test_volume_events.py::test_five_minute_step_sums_the_minutes_of_the_profile
PASSED tests/test_volume_events.py::test_scan_orders_by_multiple_not_by_rubles
PASSED tests/test_volume_events.py::test_scan_skips_quiet_tickers
PASSED tests/test_volume_events.py::test_rates_report_base_frequency
PASSED tests/test_volume_events.py::test_scan_survives_junk
PASSED tests/test_volume_events.py::test_no_verdict_fields
PASSED tests/test_volume_events.py::test_measured_flatness_of_rvol_is_written_next_to_the_code
PASSED tests/test_volume_events.py::test_thresholds_are_marked_as_guesses
PASSED tests/test_volume_events.py::test_rubles_are_documented_as_an_approximation
PASSED tests/test_volume_events.py::test_scanner_endpoint_exists
PASSED tests/test_volume_events.py::test_profile_is_built_in_background_from_past_days_only
PASSED tests/test_volume_events.py::test_page_has_a_second_table_and_names_the_baseline
PASSED tests/test_volume_events.py::test_huge_multiple_on_tiny_turnover_is_not_an_event
PASSED tests/test_volume_events.py::test_the_exact_acceleration_series_from_the_screen
PASSED tests/test_volume_events.py::test_floor_scales_with_step_length
PASSED tests/test_volume_events.py::test_real_money_still_passes
PASSED tests/test_volume_events.py::test_floor_is_configurable_without_deploy
PASSED tests/test_volume_events.py::test_suppressed_count_is_reported
PASSED tests/test_volume_events.py::test_weekend_days_do_not_count
PASSED tests/test_volume_events.py::test_short_days_do_not_count
PASSED tests/test_volume_events.py::test_unparseable_day_key_is_dropped_not_crashed
PASSED tests/test_volume_events.py::test_thin_baseline_is_marked_and_not_called_a_multiple
PASSED tests/test_volume_events.py::test_normal_baseline_keeps_the_multiple
PASSED tests/test_volume_events.py::test_morning_bars_are_not_a_baseline_for_the_main_session
PASSED tests/test_volume_events.py::test_not_enough_own_session_bars_means_no_event
PASSED tests/test_volume_events.py::test_surge_inside_one_session_still_fires
PASSED tests/test_volume_events.py::test_warming_up_is_counted
PASSED tests/test_volume_events.py::test_thin_threshold_is_not_the_floor
PASSED tests/test_volume_events.py::test_thin_does_not_scale_with_step
PASSED tests/test_volume_events.py::test_absurd_multiple_is_still_caught
PASSED tests/test_volume_events.py::test_real_awakening_keeps_its_multiple
PASSED tests/test_volume_events.py::test_thin_base_has_no_multiple_field_at_all
PASSED tests/test_volume_events.py::test_accelerating_on_thin_base_also_loses_the_multiple
PASSED tests/test_volume_events.py::test_thin_ticker_is_not_ranked_above_real_money
PASSED tests/test_volume_events.py::test_normal_money_keeps_the_multiple_field
PASSED tests/test_volume_events.py::test_profile_gap_agrees_with_the_real_filter_at_the_boundary
PASSED tests/test_volume_events.py::test_profile_gap_names_weekends_and_short_days
PASSED tests/test_volume_events.py::test_profile_gap_says_when_there_is_nothing_at_all
PASSED tests/test_volume_events.py::test_unparseable_day_is_not_counted_as_a_trading_day
PASSED tests/test_volume_events.py::test_the_note_carries_the_numbers_not_just_words
PASSED tests/test_volume_events.py::test_the_note_says_when_there_is_nothing_at_all
PASSED tests/test_volume_events.py::test_the_builder_asks_for_the_reason_and_stays_short
PASSED tests/test_profile_note.py::test_nothing_built_keeps_the_old_explanation
PASSED tests/test_profile_note.py::test_no_history_at_all_says_so
PASSED tests/test_profile_note.py::test_all_built_never_claims_it_is_missing
PASSED tests/test_profile_note.py::test_partly_built_names_both_sides
PASSED tests/test_profile_note.py::test_total_is_derived_when_not_given
PASSED tests/test_profile_note.py::test_the_note_is_written_even_when_profiles_exist
PASSED tests/test_profile_note.py::test_the_note_is_not_hidden_behind_the_empty_branch
PASSED tests/test_floor_reason.py::test_default_floor_is_fifty_thousand
PASSED tests/test_floor_reason.py::test_the_floor_is_not_explained_by_someones_deposit
PASSED tests/test_floor_reason.py::test_env_still_overrides_the_default
PASSED tests/test_floor_reason.py::test_floor_still_scales_with_step
PASSED tests/test_floor_reason.py::test_the_explanation_names_the_variable_not_a_person
PASSED tests/test_floor_reason.py::test_value_matches_the_source_when_env_is_not_set
PASSED tests/test_api_profile_note.py::test_the_reason_is_reported_next_to_profiles_ready
PASSED tests/test_api_profile_note.py::test_the_absent_attribute_cannot_break_the_route
73 passed in 0.50s
```

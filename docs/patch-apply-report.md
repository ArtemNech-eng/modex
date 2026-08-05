# Отчёт patch-apply

дата: 2026-08-05T19:52:22Z
коммит: f87f1683c2ad7ba592c26a489e2bb92648f84f6c
запуск: 31041393937
код патча: 0
код pytest: 1

## патч
```
== scripts/_patch_scanner.py
часы и непрерывность: применено
событие только на свежем баре: применено
ускорение только подряд: применено
возраст во всплеске: применено
возраст в ускорении: применено
порог возраста в DEFAULTS: применено
detect протягивает время: применено
scan принимает время: применено
scan протягивает время: применено
счётчик устаревших: применено
итог: применено 10, отказов 0
Скрипт удалил себя.
```

## pytest, последние 12000 символов
```
� ровно случай VSEH ×1510.
        rows = [bar(i, 8, close=100.0) for i in range(12)]
        rows += [bar(12, 13000, close=100.0), bar(13, 13000, close=100.0)]
        got = [e for e in detect_step(rows, 1, lot=1) if e["kind"] == "volume_surge"]
>       assert got, "оборот прошёл пол"
E       AssertionError: оборот прошёл пол
E       assert []

tests/test_volume_events.py:652: AssertionError
____________________ test_real_awakening_keeps_its_multiple ____________________

    def test_real_awakening_keeps_its_multiple():
        """
        LENT ×236.2 при норме 33 828 ₽ и обороте 7.99 млн ₽ — настоящее пробуждение
        бумаги, и кратность здесь информативна. Прежний порог её скрывал.
        """
        rows = [bar(i, 340, close=100.0) for i in range(12)]        # норма 34 тыс ₽
        rows += [bar(12, 80000, close=100.0), bar(13, 80000, close=100.0)]
        got = [e for e in detect_step(rows, 1, lot=1) if e["kind"] == "volume_surge"]
>       assert got and got[0]["base_thin"] is False, "34 тыс — не шум"
E       AssertionError: 34 тыс — не шум
E       assert ([])

tests/test_volume_events.py:667: AssertionError
_________________ test_thin_base_has_no_multiple_field_at_all __________________

    def test_thin_base_has_no_multiple_field_at_all():
        """
        04.08 на живом экране: ASTR times:30.0 при base_rub:11223, RASP
        times:28.77 при base_rub:8945. Текст честно говорил «кратность считать
        не по чему», а поле рядом говорило обратное. Побеждает поле: его
        читают программы.
        """
        rows = [bar(i, 35, close=100.0) for i in range(12)]       # норма 3 500 ₽
        rows += [bar(12, 8936, close=100.0), bar(13, 8936, close=100.0)]
        got = [e for e in detect_step(rows, 1, lot=1) if e["kind"] == "volume_surge"]
>       assert got
E       assert []

tests/test_volume_events.py:681: AssertionError
_______________ test_thin_ticker_is_not_ranked_above_real_money ________________

    def test_thin_ticker_is_not_ranked_above_real_money():
        """
        ГЛАВНОЕ ПОСЛЕДСТВИЕ. Сортировка шла по times, и верх доски занимал
        шум: ASTR ×30 на 11 тыс ₽ стоял выше всего настоящего.
        """
        real = steady(12, vol=1000, close=100.0)                 # норма 100 тыс ₽
        real += [bar(12, 5000, 100.0), bar(13, 5000, 100.0)]     # 500 тыс ₽, ×5
        thin = [bar(i, 35, close=100.0) for i in range(12)]      # норма 3 500 ₽
        thin += [bar(12, 8936, close=100.0), bar(13, 8936, close=100.0)]
        got = scan({"REAL": real, "THIN": thin}, lots={"REAL": 1, "THIN": 1})
>       assert [x["ticker"] for x in got][0] == "REAL", "шум не возглавляет доску"
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E       IndexError: list index out of range

tests/test_volume_events.py:711: IndexError
__________________ test_normal_money_keeps_the_multiple_field __________________

    def test_normal_money_keeps_the_multiple_field():
        real = steady(12, vol=1000, close=100.0)
        real += [bar(12, 5000, 100.0), bar(13, 5000, 100.0)]
        got = scan({"REAL": real}, lots={"REAL": 1})
>       assert got[0]["max_times"] >= 3
               ^^^^^^
E       IndexError: list index out of range

tests/test_volume_events.py:722: IndexError
==================================== PASSES ====================================
=========================== short test summary info ============================
PASSED tests/test_volume_events.py::test_forming_bar_is_ignored
PASSED tests/test_volume_events.py::test_too_little_history
PASSED tests/test_volume_events.py::test_no_baseline_no_events
PASSED tests/test_volume_events.py::test_ordinary_volume_is_not_a_surge
PASSED tests/test_volume_events.py::test_growth_from_nothing_is_not_acceleration
PASSED tests/test_volume_events.py::test_growth_must_be_uninterrupted
PASSED tests/test_volume_events.py::test_time_of_day_profile_needs_enough_days
PASSED tests/test_volume_events.py::test_source_of_the_baseline_is_always_reported
PASSED tests/test_volume_events.py::test_five_minute_step_sums_the_minutes_of_the_profile
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
PASSED tests/test_volume_events.py::test_suppressed_count_is_reported
PASSED tests/test_volume_events.py::test_weekend_days_do_not_count
PASSED tests/test_volume_events.py::test_short_days_do_not_count
PASSED tests/test_volume_events.py::test_unparseable_day_key_is_dropped_not_crashed
PASSED tests/test_volume_events.py::test_morning_bars_are_not_a_baseline_for_the_main_session
PASSED tests/test_volume_events.py::test_not_enough_own_session_bars_means_no_event
PASSED tests/test_volume_events.py::test_warming_up_is_counted
PASSED tests/test_volume_events.py::test_thin_threshold_is_not_the_floor
PASSED tests/test_volume_events.py::test_accelerating_on_thin_base_also_loses_the_multiple
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
PASSED tests/test_scanner_freshness.py::test_fresh_bar_still_gives_an_event
PASSED tests/test_scanner_freshness.py::test_a_two_hour_old_bar_is_not_an_event
PASSED tests/test_scanner_freshness.py::test_the_age_is_the_reason_and_not_something_else
PASSED tests/test_scanner_freshness.py::test_the_event_carries_its_age
PASSED tests/test_scanner_freshness.py::test_a_bar_marked_one_minute_ahead_is_not_dropped
PASSED tests/test_scanner_freshness.py::test_a_real_run_up_is_still_caught
PASSED tests/test_scanner_freshness.py::test_a_run_up_with_a_hole_in_it_is_not_a_run_up
PASSED tests/test_scanner_freshness.py::test_evening_and_next_morning_are_not_consecutive
PASSED tests/test_scanner_freshness.py::test_minute_by_minute_is_contiguous
PASSED tests/test_scanner_freshness.py::test_silence_from_staleness_is_counted
PASSED tests/test_scanner_freshness.py::test_scan_passes_the_clock_down
PASSED tests/test_scanner_freshness.py::test_without_a_clock_it_uses_moscow_time
PASSED tests/test_api_profile_note.py::test_the_reason_is_reported_next_to_profiles_ready
PASSED tests/test_api_profile_note.py::test_the_absent_attribute_cannot_break_the_route
FAILED tests/test_volume_events.py::test_turnover_is_in_rubles_with_lot_size - assert ([])
FAILED tests/test_volume_events.py::test_price_enters_the_turnover - IndexError: list index out of range
FAILED tests/test_volume_events.py::test_baseline_skips_empty_minutes - AssertionError: норма по ненулевым
assert ([])
FAILED tests/test_volume_events.py::test_baseline_excludes_the_measured_bar - assert ([])
FAILED tests/test_volume_events.py::test_surge_is_the_case_from_the_request - assert []
FAILED tests/test_volume_events.py::test_acceleration_matches_the_example - assert []
FAILED tests/test_volume_events.py::test_one_spike_is_not_acceleration - AssertionError: assert 'volume_surge' in []
FAILED tests/test_volume_events.py::test_time_of_day_profile_is_used_when_present - assert ([])
FAILED tests/test_volume_events.py::test_scan_orders_by_multiple_not_by_rubles - IndexError: list index out of range
FAILED tests/test_volume_events.py::test_scan_skips_quiet_tickers - AssertionError: assert [] == ['LOUD']
  
  Right contains one more item: 'LOUD'
  
  Full diff:
  + []
  - [
  -     'LOUD',
  - ]
FAILED tests/test_volume_events.py::test_rates_report_base_frequency - KeyError: 'volume_surge'
FAILED tests/test_volume_events.py::test_floor_scales_with_step_length - AssertionError: минутка проходит пол 1000
assert []
 +  where [] = detect_step([{'ts': '2026-08-03T10:00', 'open': 100.0, 'high': 100.0, 'low': 100.0, ...}, {'ts': '2026-08-03T10:01', 'open': 100.0...0, 'high': 100.0, 'low': 100.0, ...}, {'ts': '2026-08-03T10:05', 'open': 100.0, 'high': 100.0, 'low': 100.0, ...}, ...], 1, lot=1, p={'floor': 1000.0})
FAILED tests/test_volume_events.py::test_real_money_still_passes - assert ([])
FAILED tests/test_volume_events.py::test_floor_is_configurable_without_deploy - AssertionError: с низким полом видно
assert []
 +  where [] = detect_step([{'ts': '2026-08-03T10:00', 'open': 100.0, 'high': 100.0, 'low': 100.0, ...}, {'ts': '2026-08-03T10:01', 'open': 100.0...0, 'high': 100.0, 'low': 100.0, ...}, {'ts': '2026-08-03T10:05', 'open': 100.0, 'high': 100.0, 'low': 100.0, ...}, ...], 1, lot=1, p={'floor': 1000.0})
FAILED tests/test_volume_events.py::test_thin_baseline_is_marked_and_not_called_a_multiple - assert []
FAILED tests/test_volume_events.py::test_normal_baseline_keeps_the_multiple - assert ([])
FAILED tests/test_volume_events.py::test_surge_inside_one_session_still_fires - assert ([])
FAILED tests/test_volume_events.py::test_thin_does_not_scale_with_step - AssertionError: 100 тыс на минутном баре — не шум
assert ([])
FAILED tests/test_volume_events.py::test_absurd_multiple_is_still_caught - AssertionError: оборот прошёл пол
assert []
FAILED tests/test_volume_events.py::test_real_awakening_keeps_its_multiple - AssertionError: 34 тыс — не шум
assert ([])
FAILED tests/test_volume_events.py::test_thin_base_has_no_multiple_field_at_all - assert []
FAILED tests/test_volume_events.py::test_thin_ticker_is_not_ranked_above_real_money - IndexError: list index out of range
FAILED tests/test_volume_events.py::test_normal_money_keeps_the_multiple_field - IndexError: list index out of range
23 failed, 62 passed in 0.65s
```

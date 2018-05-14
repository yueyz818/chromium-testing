#!/usr/bin/env python
# Copyright 2017 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Runs several telemetry benchmarks.

This script attempts to emulate the contract of gtest-style tests
invoked via recipes. The main contract is that the caller passes the
argument:

  --isolated-script-test-output=[FILENAME]

json is written to that file in the format detailed here:
https://www.chromium.org/developers/the-json-test-results-format

Optional argument:

  --isolated-script-test-filter=[TEST_NAMES]

is a double-colon-separated ("::") list of test names, to run just that subset
of tests. This list is forwarded to the run_telemetry_benchmark_as_googletest
script.

This script is intended to be the base command invoked by the isolate,
followed by a subsequent Python script. It could be generalized to
invoke an arbitrary executable.

It currently runs several benchmarks. The benchmarks it will execute are
based on the shard it is running on and the sharding_map_path.

If this is executed with a non-telemetry perf test, the flag --non-telemetry
has to be passed in to the script so the script knows it is running
an executable and not the run_benchmark command.

The results of running the benchmark are put in separate directories per
benchmark. Two files will be present in each directory; perf_results.json, which
is the perf specific results (with unenforced format, could be histogram,
legacy, or chartjson), and test_results.json, which is a JSON test results
format file
(https://www.chromium.org/developers/the-json-test-results-format)

This script was derived from run_telemetry_benchmark_as_googletest, and calls
into that script.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import traceback

import common

import run_telemetry_benchmark_as_googletest
import run_gtest_perf_test

# Current whitelist of benchmarks outputting histograms
BENCHMARKS_TO_OUTPUT_HISTOGRAMS = [
    'dummy_benchmark.histogram_benchmark_1',
    'blink_perf.bindings',
    'blink_perf.canvas',
    'blink_perf.css',
    'blink_perf.dom',
    'blink_perf.events',
    'blink_perf.image_decoder',
    'blink_perf.layout',
    'blink_perf.owp_storage',
    'blink_perf.paint',
    'blink_perf.parser',
    'blink_perf.shadow_dom',
    'blink_perf.svg',
    'memory.top_10_mobile'
]

# We currently have two different sharding schemes for android
# vs desktop.  When we are running at capacity we will have 26
# desktop shards and 39 android.
CURRENT_DESKTOP_NUM_SHARDS = 26
CURRENT_ANDROID_NUM_SHARDS = 39

def get_sharding_map_path(total_shards, testing):
  # Determine if we want to do a test run of the benchmarks or run the
  # full suite.
  if not testing:
    # Note: <= for testing purposes until we have all shards running
    if int(total_shards) <= CURRENT_DESKTOP_NUM_SHARDS:
      return os.path.join(
          os.path.dirname(__file__), '..', '..', 'tools', 'perf', 'core',
          'benchmark_desktop_bot_map.json')
    else:
      return os.path.join(
          os.path.dirname(__file__), '..', '..', 'tools', 'perf', 'core',
          'benchmark_android_bot_map.json')
  else:
    return os.path.join(
      os.path.dirname(__file__), '..', '..', 'tools', 'perf', 'core',
      'benchmark_bot_map.json')


def write_results(
    perf_test_name, perf_results, json_test_results, isolated_out_dir, encoded):
  benchmark_path = os.path.join(isolated_out_dir, perf_test_name)

  os.makedirs(benchmark_path)
  with open(os.path.join(benchmark_path, 'perf_results.json'), 'w') as f:
    # non telemetry perf results are already json encoded
    if encoded:
      f.write(perf_results)
    else:
      json.dump(perf_results, f)
  with open(os.path.join(benchmark_path, 'test_results.json'), 'w') as f:
    json.dump(json_test_results, f)


def execute_benchmark(benchmark, isolated_out_dir,
                      args, rest_args, is_reference):
  # While we are between chartjson and histogram set we need
  # to determine which output format to look for or see if it was
  # already passed in in which case that format applies to all benchmarks
  # in this run.
  is_histograms = append_output_format(benchmark, args, rest_args)
  # Insert benchmark name as first argument to run_benchmark call
  # which is the first argument in the rest_args.  Also need to append
  # output format.
  per_benchmark_args = (rest_args[:1] + [benchmark] + rest_args[1:])
  benchmark_name = benchmark
  if is_reference:
    # Need to parse out the browser to replace browser flag with
    # reference build so we run it reference build as well
    browser_index = 0
    for arg in per_benchmark_args:
      if "browser" in arg:
        break
      browser_index = browser_index + 1
    per_benchmark_args[browser_index] = '--browser=reference'
    # Now we need to add in the rest of the reference build args
    per_benchmark_args.append('--max-failures=5')
    per_benchmark_args.append('--output-trace-tag=_ref')
    benchmark_name = benchmark + '.reference'

  # We don't care exactly what these are. In particular, the perf results
  # could be any format (chartjson, legacy, histogram). We just pass these
  # through, and expose these as results for this task.
  rc, perf_results, json_test_results = (
      run_telemetry_benchmark_as_googletest.run_benchmark(
          args, per_benchmark_args, is_histograms))

  write_results(
      benchmark_name, perf_results, json_test_results, isolated_out_dir, False)
  return rc


def append_output_format(benchmark, args, rest_args):
  # We need to determine if the output format is already passed in
  # or if we need to define it for this benchmark
  perf_output_specified = False
  is_histograms = False
  if args.output_format:
    for output_format in args.output_format:
      if 'histograms' in output_format:
        perf_output_specified = True
        is_histograms = True
      if 'chartjson' in output_format:
        perf_output_specified = True
      rest_args.append('--output-format=' + output_format)
  # When crbug.com/744736 is resolved we no longer have to check
  # the type of format per benchmark and can rely on it being passed
  # in as an arg as all benchmarks will output the same format.
  if not perf_output_specified:
    if benchmark in BENCHMARKS_TO_OUTPUT_HISTOGRAMS:
      rest_args.append('--output-format=histograms')
      is_histograms = True
    else:
      rest_args.append('--output-format=chartjson')
  return is_histograms

def main():
  parser = argparse.ArgumentParser()
  parser.add_argument(
      '--isolated-script-test-output', required=True)
  # These two flags are passed in from the swarming recipe
  # but will no longer be needed when we migrate to this new recipe.
  # For now we need to recognize them so they don't get passed
  # through to telemetry.
  parser.add_argument(
      '--isolated-script-test-chartjson-output', required=False)
  parser.add_argument(
      '--isolated-script-test-perf-output', required=False)

  parser.add_argument(
      '--isolated-script-test-filter', type=str, required=False)
  parser.add_argument('--xvfb', help='Start xvfb.', action='store_true')
  parser.add_argument('--non-telemetry',
                      help='Type of perf test', type=bool, default=False)
  parser.add_argument('--testing', help='Test run, execute subset of tests',
                      type=bool, default=False)
  parser.add_argument('--benchmarks',
                      help='Comma separated list of benchmark names'
                      ' to run in lieu of indexing into our benchmark bot maps',
                      required=False)
  parser.add_argument('--output-format', action='append')

  args, rest_args = parser.parse_known_args()
  isolated_out_dir = os.path.dirname(args.isolated_script_test_output)
  return_code = 0

  if args.non_telemetry:
    # For non telemetry tests the benchmark name is the name of the executable.
    benchmark_name = rest_args[0]
    return_code, charts, output_json = run_gtest_perf_test.execute_perf_test(
        args, rest_args)

    write_results(benchmark_name, charts, output_json, isolated_out_dir, True)
  else:
    # If the user has supplied a list of benchmark names, execute those instead
    # of the entire suite of benchmarks.
    if args.benchmarks:
      benchmarks = args.benchmarks.split(',')
      for benchmark in benchmarks:
        return_code = (execute_benchmark(
            benchmark, isolated_out_dir, args, rest_args, False) or return_code)
    else:
      # First determine what shard we are running on to know how to
      # index into the bot map to get list of benchmarks to run.
      total_shards = None
      shard_index = None

      env = os.environ.copy()
      if 'GTEST_TOTAL_SHARDS' in env:
        total_shards = env['GTEST_TOTAL_SHARDS']
      if 'GTEST_SHARD_INDEX' in env:
        shard_index = env['GTEST_SHARD_INDEX']

      if not (total_shards or shard_index):
        raise Exception('Shard indicators must be present for perf tests')

      sharding_map_path = get_sharding_map_path(
          total_shards, args.testing or False)
      with open(sharding_map_path) as f:
        sharding_map = json.load(f)
      sharding = None
      sharding = sharding_map[shard_index]['benchmarks']

      # We don't execute tests on the reference build on android webview
      # since telemetry doesn't support it.  See crbug.com/612455
      is_webview = any(('browser' in a and 'webview' in a) for a in rest_args)

      for benchmark in sharding:
        # Need to run the benchmark twice on browser and reference build
        return_code = (execute_benchmark(
            benchmark, isolated_out_dir, args, rest_args, False) or return_code)
        # We ignore the return code of the reference build since we do not
        # monitor it.
        if not is_webview:
          execute_benchmark(benchmark, isolated_out_dir, args, rest_args, True)

  return return_code


# This is not really a "script test" so does not need to manually add
# any additional compile targets.
def main_compile_targets(args):
  json.dump([], args.output)


if __name__ == '__main__':
  # Conform minimally to the protocol defined by ScriptTest.
  if 'compile_targets' in sys.argv:
    funcs = {
      'run': None,
      'compile_targets': main_compile_targets,
    }
    sys.exit(common.run_script(sys.argv[1:], funcs))
  sys.exit(main())

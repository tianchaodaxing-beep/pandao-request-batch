# PANDAO batch web queries

[简体中文](README.md) · English

Turn one captured web read request into a batch driven by a CSV parameter table. Read pages automatically, save progress, retry temporary failures and export a spreadsheet with a task summary.

For example, query a directory once by category, export the browser capture, then list the other categories in a CSV. The tool reads each category without repeating the page clicks.

[Source](https://github.com/tianchaodaxing-beep/pandao-request-batch) · [Download](https://github.com/tianchaodaxing-beep/pandao-request-batch/releases/latest)

## Install and open

Requires Python 3.11 or later. Download and extract the ZIP. On Windows, double-click `Open-batch-queries.cmd`, or run:

```sh
python -m pip install .
python -m pandao_batch --lang en serve --open
```

Click **English** in the page header to switch the interface; click **中文** to return. Your current inputs are retained. The page listens only on your own device. Runtime dependencies are the Python standard library; installation uses setuptools. No language model or paid API is required.

Click **Load simulated demo**, then **Run batch queries** to try 12 queries. This uses a local simulated service, not real customer data or evidence of commercial results.

## Use your own captured query

1. Open a website you are allowed to read. Open developer tools, choose Network and perform one normal query.
2. Export a HAR capture that includes response content. Browser menu labels vary by version.
3. Import the capture. Select the read source, record location, source count, pagination parameter and unique ID field.
4. Import or enter a CSV parameter table. Headers must match the query parameters in the captured request. Each row is a query. Do not include the pagination column; the tool controls it.
5. Run the batch and download the results. Failed tasks retain saved pages for resumption after you resolve the error.

Captures and local configurations may contain login information. Keep them on your device and out of public repositories. Default browser exports may omit authentication. A read requiring login needs valid local headers or cookies. The tool does not log in, refresh dynamic signatures, solve CAPTCHAs or work around access restrictions.

## Supported scope

Supports captured **GET** requests returning JSON with an array of object records, and pagination by increasing ordinary page numbers. Source totals and a unique ID field are optional.

POST queries, cursor pagination, live dynamic signatures and browser-only execution are not supported. A capture cannot establish all of a website's business rules. Some sites use GET for actions: choose a source that only reads data.

Useful for directory queries, public information searches and exports from systems you already have permission to access. Compatibility depends on the actual site. Validation so far uses the included local simulated HTTP service, without a real customer site acceptance test.

## Progress and checks

- Each checked page is saved. Temporary network failures and rate limiting are retried at most twice by default; this is configurable.
- Resume with the same configuration, authentication, parameter table and start page. Changing any of those requires a new result directory. Page and record limits can be increased.
- Deduplication is separate for each query, using the selected ID or the entire record. Different queries retain their own results.
- When a source count is available, check it per query. Changed totals, repeated pages, changed record structure or inconsistent counts stop completion.
- Without a source count, an empty page ends pagination. Reaching the page limit while data remains is incomplete. A non-paginated read only establishes that one page was read.
- Saved page content is checked against progress records. CSV and JSONL exports are independently read back to check their row counts.

Matching counts cannot establish that records were not replaced during pagination. Use a stable dataset or a site-provided snapshot. Collection time is recorded separately; if the source provides no update time, the summary says so.

Results are not sanitized. CSV uses UTF-8 with a byte-order mark for Excel. JSONL retains nested data. Every exported record includes its query number and page number. Source fields, IDs and parameter names are not translated. A separate `Summary.en.md` describes task metadata in English; original machine-readable files retain their schema.

## Command line

Use `--lang zh` or `--lang en` before the subcommand for help, status and errors. Command names and machine-readable JSON are unchanged.

```sh
python -m pandao_batch --lang en inspect capture.har
python -m pandao_batch --lang en prepare capture.har --entry 0 --out read-config.json --records-pointer /data/items --total-pointer /data/total --page-param page --id-field id
python -m pandao_batch --lang en run read-config.json queries.csv --out results
python -m pandao_batch --lang en run read-config.json queries.csv --out results --resume --max-pages 200
python -m pandao_batch --lang en demo --out simulated-demo
```

The source index and pointers above illustrate the syntax: use those present in your own capture. Other options include `--interval 0.2`, `--retries 2`, `--timeout 15`, `--max-seconds 600` and `--max-records 100000`. A saved configuration and parameter table make subsequent tasks a single command.

## Implementation and license

Independently implemented by PANDAO, licensed under MIT. The network observation and deterministic interface direction was informed by [OpenCLI](https://github.com/jackwener/OpenCLI); no OpenCLI code is copied or imported, and that existing direction is not claimed as an invention here.

```sh
python -m pip install . pytest
python -m pytest -q
```

# Loading and response performance

Pages render their navigation, filters, and placeholders immediately. Broker-account data is loaded through `/api/account/view`; market expiry data uses `/api/expiries`. Neither page shell waits for an external broker.

`static/loading.js` provides accessible region skeletons, refresh indicators, action feedback, bounded requests, and explicit download progress. Initial failures replace skeletons with a retry control. Refreshes preserve existing content. Stale date-range responses cannot replace the latest selection, and repeated action clicks are ignored while that action is pending. Native application disabled states and live confirmations remain authoritative.

Only simultaneous GET requests for the small broker, overview, and stocks snapshots are shared within the current page. Results are not retained or written to browser storage. Mutations are never automatically retried. Order reconciliation continues in the server worker; the Overview reads saved evidence rather than starting a second broker poll.

Static files with the release version query are immutable and cacheable. Bump every affected asset reference and the matching middleware version on future releases. HTML, APIs, authentication responses, and errors remain `no-store`. Gzip is enabled for larger responses. System fonts replace a blocking remote font import; the chart library loads only when a chart is needed.

Validate with the Python tests, JavaScript tests, and browser checks at desktop and mobile widths. Include initial loading, successful empty/populated data, failed requests, manual retry, overlapping date selection, and refresh without losing controls or previous results.

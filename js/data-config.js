/**
 * Data Source Configuration
 *
 * NOTE: this file is NOT generated. Earlier revisions implied it was
 * auto-generated and injected by GitHub Actions, which led to a broken
 * site: when the injection step did not run (a cancelled workflow, or a
 * run where deduplication skipped the later steps), the placeholder text
 * below was deployed verbatim and every data request 404'd, leaving the
 * page stuck on "Loading papers...".
 *
 * The values are therefore hardcoded. If this repository is renamed or
 * transferred, update repoOwner/repoName here. The CI injection step no
 * longer touches this file.
 */

const DATA_CONFIG = {
    /**
     * GitHub repository owner (username)
     */
    repoOwner: 'Hsintao',

    /**
     * GitHub repository name
     */
    repoName: 'daily-arXiv-ai-enhanced',

    /**
     * Data branch name
     * Default: 'data'
     */
    dataBranch: 'data',

    /**
     * Get the base URL for raw GitHub content from data branch
     * @returns {string} Base URL for raw GitHub content
     */
    getDataBaseUrl: function() {
        return `https://raw.githubusercontent.com/${this.repoOwner}/${this.repoName}/${this.dataBranch}`;
    },

    /**
     * Get the full URL for a data file
     * @param {string} filePath - Relative path to the data file (e.g., 'data/2025-01-01.jsonl')
     * @returns {string} Full URL to the data file
     */
    getDataUrl: function(filePath) {
        return `${this.getDataBaseUrl()}/${filePath}`;
    }
};

/**
 * Topic boosting for low-level vision and computational photography.
 *
 * Papers matching any group are sorted to the top of the list. Nothing is
 * hidden -- the site still shows every paper, matching ones just come first.
 *
 * Editing this list is the supported way to tune results: add or remove terms
 * under TOPIC_GROUPS, reload the page, and the change takes effect. There is no
 * server involved.
 *
 * Matching rules
 * --------------
 * - Case-insensitive.
 * - Word-boundary matching, not substring. This matters: plain
 *   `includes('isp')` matches "disparity" and "display", which produced 30
 *   false positives out of 30 in a sample of real papers. Likewise `hdr`
 *   substring-matches "HDR Prostate Brachytherapy" (a radiotherapy paper).
 * - Hyphens and spaces are interchangeable, so "super-resolution" also
 *   matches "super resolution".
 * - AI summary fields are searched too, not just the English title and
 *   abstract. A paper titled "Restoring Clarity in Low-Light Scenes" carries
 *   the Chinese term 去噪/超分 in its generated summary even when the English
 *   title never says "denoising".
 *
 * Deliberately excluded, despite frequent hits in the data:
 *   pipeline (155)  - "training pipeline", "labeling pipeline": ML plumbing.
 *   capture  (204)  - "capture dependencies", "capture interactions": metaphor.
 *   denoising-alone - diffusion models call their sampling step "denoising",
 *                     so 18 of 58 hits were generative-model papers rather
 *                     than image restoration. Prefer compound terms below.
 */

const TOPIC_GROUPS = [
  {
    id: 'denoising',
    label: '去噪 Denoising',
    terms: [
      'denoising',
      'denoise',
      'noise reduction',
      'raw denoising',
      'sensor noise',
    ],
  },
  {
    id: 'superres',
    label: '超分 Super-resolution',
    terms: [
      'super-resolution',
      'superresolution',
      'super resolution',
      'upsampling',
      'image resizing',
    ],
  },
  {
    id: 'deblur',
    label: '去模糊 Deblurring',
    terms: [
      'deblurring',
      'motion blur',
      'defocus blur',
      'motion deblurring',
      'blur kernel',
    ],
  },
  {
    id: 'weather',
    label: '去雨去雾 Deraining / Dehazing',
    terms: [
      'deraining',
      'dehazing',
      'rain removal',
      'haze removal',
      'adverse weather',
      'snow removal',
    ],
  },
  {
    id: 'restoration',
    label: '修复 Restoration / Inpainting',
    terms: [
      'image restoration',
      'inpainting',
      'image completion',
      'old photo restoration',
      'image retouching',
      'scratch removal',
    ],
  },
  {
    id: 'lowlight',
    label: '低光增强 Low-light',
    terms: [
      'low-light',
      'low light',
      'image enhancement',
      'exposure correction',
      'illumination enhancement',
      'night photography',
      'underexposed',
    ],
  },
  {
    id: 'degradation',
    label: '其他退化 Other degradation',
    terms: [
      'demosaicing',
      'compression artifact',
      'jpeg artifact',
      'artifact removal',
      'quantization artifact',
    ],
  },
  {
    id: 'compphoto',
    label: '计算摄像 Computational photography',
    terms: [
      'computational photography',
      'computational imaging',
      'image signal processing',
      'camera pipeline',
      'imaging pipeline',
      'raw image',
      'camera raw',
      'bayer pattern',
      'white balance',
      'color constancy',
    ],
  },
  {
    id: 'hdr',
    label: 'HDR / 光照 Relighting',
    terms: [
      'high dynamic range',
      'hdr imaging',
      'hdr image',
      'relighting',
      'photometric stereo',
      'intrinsic decomposition',
      'inverse rendering',
    ],
  },
  {
    id: 'optics',
    label: '光学成像 Optics',
    terms: [
      'light field',
      'lensless',
      'single-photon',
      'coded aperture',
      'time-of-flight',
      'diffractive optics',
      'meta-lens',
    ],
  },
  {
    id: 'camera',
    label: '相机 / 拍摄 Camera',
    terms: [
      'camera calibration',
      'autofocus',
      'depth of field',
      'burst photography',
      'rolling shutter',
      'image stabilization',
      'exposure bracketing',
    ],
  },
  {
    id: 'threed',
    label: '3D / 新视角 3D & Novel View',
    terms: [
      'neural radiance field',
      'nerf',
      '3d reconstruction',
      'depth estimation',
      'stereo matching',
      'novel view synthesis',
      'gaussian splatting',
      'multi-view stereo',
    ],
  },
];

/**
 * Phrases that disqualify a paper from a group even when a term matches.
 *
 * Trade-off: this only catches cases where the disambiguating context is
 * present in the text. It reduces false positives, it does not eliminate them.
 */
const TOPIC_EXCLUDES = {
  denoising: ['denoising diffusion', 'diffusion model denoising', 'denoising score'],
};

/**
 * Groups where a generative-model vocabulary means the term is being used in
 * the diffusion/generation sense rather than the image-restoration sense.
 *
 * "Denoising" is the sampling step of every diffusion model, so papers like
 * "Adaptive Patch-wise Denoising for Fast Autoregressive Video Generation" match
 * the denoising term while having nothing to do with image restoration. In a
 * sample of 2026-10-14, 5 of 8 denoising hits were generative papers.
 *
 * A paper is dropped from the group only when generative vocabulary is present
 * AND no imaging vocabulary is. A paper about both (e.g. a denoiser used as a
 * diffusion prior) is kept, since it is still relevant.
 */
const GENERATIVE_CONTEXT = new RegExp(
  '(?<![a-z0-9])(diffusion|autoregressive|generative|generation|latent|' +
    'text-to-image|text-to-video|video generation|image generation|' +
    'world model|sampling step)(?![a-z0-9])',
  'i'
);

const IMAGING_CONTEXT = new RegExp(
  '(?<![a-z0-9])(image|photo|photograph|pixel|raw|sensor|noise level|blind|' +
    'restoration|jpeg|camera|denoiser|psnr|ssim)(?![a-z0-9])',
  'i'
);

const GENERATIVE_SENSITIVE_GROUPS = ['denoising'];

/** Build a case-insensitive word-boundary regex from a term list. */
function buildTopicPattern(terms) {
  const escaped = terms.map((term) => {
    // Escape regex metacharacters FIRST, then turn the (escaped) separators
    // into a flexible class. Doing it the other way round escapes the
    // generated [ and + characters, producing a pattern that matches nothing.
    const safe = term.trim().toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return safe.replace(/[-\s]+/g, '[-\\s]+');
  });
  if (escaped.length === 0) return null;
  return new RegExp('(?<![a-z0-9])(' + escaped.join('|') + ')(?![a-z0-9])', 'i');
}

/**
 * Concatenate every field worth searching: English title/abstract plus the
 * generated Chinese summary. Falls back gracefully when AI fields are absent
 * (older imported data), so behaviour degrades to title+abstract only.
 */
function topicHaystack(paper) {
  const ai = paper.AI || {};
  return [
    paper.title || '',
    paper.summary || '',
    paper.details || '',
    typeof ai.tldr === 'string' ? ai.tldr : '',
    typeof ai.motivation === 'string' ? ai.motivation : '',
    typeof ai.method === 'string' ? ai.method : '',
    typeof ai.result === 'string' ? ai.result : '',
    typeof ai.conclusion === 'string' ? ai.conclusion : '',
  ].join(' ');
}

// Compiled once at load time rather than per paper, since the list is static.
const TOPIC_PATTERNS = TOPIC_GROUPS.map((group) => ({
  id: group.id,
  label: group.label,
  pattern: buildTopicPattern(group.terms),
  excludes: (TOPIC_EXCLUDES[group.id] || []).map(
    (phrase) => new RegExp(phrase.replace(/[-\s]+/g, '[-\\s]+'), 'i')
  ),
  generativeSensitive: GENERATIVE_SENSITIVE_GROUPS.includes(group.id),
}));

/**
 * Return the labels of every topic group this paper belongs to.
 * Empty array means the paper is unrelated to the configured topics.
 */
function matchTopics(paper) {
  const haystack = topicHaystack(paper);
  if (!haystack.trim()) return [];

  const matched = [];
  for (const topic of TOPIC_PATTERNS) {
    if (!topic.pattern || !topic.pattern.test(haystack)) continue;

    // Skip when a disqualifying phrase is present.
    if (topic.excludes.some((re) => re.test(haystack))) continue;

    // For groups where generative vocabulary hijacks the term, require some
    // imaging vocabulary to be present as well.
    if (
      topic.generativeSensitive &&
      GENERATIVE_CONTEXT.test(haystack) &&
      !IMAGING_CONTEXT.test(haystack)
    ) {
      continue;
    }

    matched.push(topic.label);
  }
  return matched;
}

/** True when the paper matches at least one configured topic. */
function isTopicPaper(paper) {
  return matchTopics(paper).length > 0;
}

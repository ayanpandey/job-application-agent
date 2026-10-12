/* ==========================================================================
   Job Agent — desktop UI controller
   ========================================================================== */

(function () {
  'use strict';

  // -- State -----------------------------------------------------------------
  const state = {
    firstRun: false,
    hasKey: false,
    licence: null,
    running: false,
    pollInterval: null,
    autoscroll: true,
    liveMode: false,
    allApplications: [],
    activeModalResume: null,
  };

  // -- DOM References -------------------------------------------------------
  const dom = {
    boot: document.getElementById('boot'),
    views: {
      licence: document.getElementById('view-licence'),
      browser: document.getElementById('view-browser'),
      wizard: document.getElementById('view-wizard'),
      dashboard: document.getElementById('view-dashboard'),
    },
    tabs: {
      dashboard: document.getElementById('tab-dashboard'),
      applications: document.getElementById('tab-applications'),
      settings: document.getElementById('tab-settings'),
    },
    navItems: document.querySelectorAll('.nav-item'),
    modal: document.getElementById('modal-resume-inspect'),
  };

  // Wait for pywebview API injection.
  //
  // pywebview's api.js defines `window.pywebview = { api: {}, ... }` immediately,
  // so `window.pywebview.api` is a truthy *empty* object well before finish.js
  // runs `_createApi()` to hang the Python methods off it. Testing only that the
  // object exists therefore passes on a cold start and init() calls into nothing:
  // "window.pywebview.api.get_state is not a function". Probe for the method we
  // actually need, and take `pywebviewready` as the authoritative signal.
  function apiReady() {
    return !!(window.pywebview
      && window.pywebview.api
      && typeof window.pywebview.api.get_state === 'function');
  }

  function waitForApi(callback, attempts = 0) {
    let started = false;
    const start = () => {
      if (started) return;   // event and poll can both fire; only boot once
      started = true;
      callback();
    };

    // The bridge fires this once the API surface is fully attached.
    window.addEventListener('pywebviewready', () => {
      if (apiReady()) start();
    });

    // Belt and braces: the event may already have fired before this script ran.
    (function poll(n) {
      if (started) return;
      if (apiReady()) { start(); return; }
      if (n < 300) {                        // 300 x 50ms = 15s
        setTimeout(() => poll(n + 1), 50);
      } else {
        dom.boot.innerHTML =
          '<p class="boot-fail">The app could not reach its backend.'
          + ' Close the window and start it again.</p>';
      }
    })(attempts);
  }

  // Switch top-level views
  function showView(name) {
    Object.values(dom.views).forEach(v => v.classList.add('hidden'));
    if (dom.views[name]) {
      dom.views[name].classList.remove('hidden');
    }
  }

  // The main header is the only place a panel is titled -- the cards below it
  // carry controls, not headings -- so it has to track the selected tab.
  // Leaving it pinned to "Run deck" contradicted whatever tab was open.
  const TAB_HEADINGS = {
    dashboard:    ['Run deck', 'Search, score, tailor, apply. You decide when it submits.'],
    applications: ['Jobs', 'Every listing the agent looked at, what it scored, and which resume it sent.'],
    settings:     ['Settings', 'Change what the agent searches for, swap your base resume, or remove the licence.'],
  };

  // Switch sidebar tabs
  function showTab(tabName) {
    const heading = TAB_HEADINGS[tabName];
    if (heading) {
      const titleEl = document.getElementById('dash-greeting');
      const subEl = document.getElementById('dash-subtext');
      if (titleEl) titleEl.textContent = heading[0];
      if (subEl) subEl.textContent = heading[1];
    }

    dom.navItems.forEach(item => {
      if (item.dataset.tab === tabName) {
        item.classList.add('active');
      } else {
        item.classList.remove('active');
      }
    });

    Object.entries(dom.tabs).forEach(([k, tabEl]) => {
      if (k === tabName) {
        tabEl.classList.remove('hidden');
        tabEl.classList.add('active');
      } else {
        tabEl.classList.add('hidden');
        tabEl.classList.remove('active');
      }
    });

    if (tabName === 'applications') loadApplications();
  }

  // =========================================================================
  // Initialization & Boot Flow
  // =========================================================================

  async function init() {
    try {
      const appState = await window.pywebview.api.get_state();
      dom.boot.classList.add('hidden');

      state.firstRun = appState.first_run;
      state.hasKey = appState.has_key;
      state.licence = appState.licence;

      if (!state.hasKey || !state.licence || !state.licence.valid) {
        setupLicenceView();
        showView('licence');
        return;
      }

      if (!appState.browser_ready) {
        setupBrowserView();
        showView('browser');
        return;
      }

      if (state.firstRun) {
        setupWizardView();
        showView('wizard');
        return;
      }

      setupDashboardView();
      showView('dashboard');
    } catch (err) {
      console.error('Boot error:', err);
      dom.boot.innerHTML = `<p style="color:#ef4444;padding:20px;">Startup Error: ${err}</p>`;
    }
  }

  // =========================================================================
  // Licence View
  // =========================================================================

  function setupLicenceView() {
    const keyInput = document.getElementById('licence-key');
    const submitBtn = document.getElementById('licence-submit');
    const errBanner = document.getElementById('licence-error');

    submitBtn.onclick = async () => {
      const key = keyInput.value.trim();
      if (!key) return;
      submitBtn.disabled = true;
      submitBtn.textContent = 'Activating…';
      errBanner.classList.add('hidden');

      try {
        const res = await window.pywebview.api.activate_licence(key);
        if (res.ok) {
          state.hasKey = true;
          state.licence = res.licence;
          const appState = await window.pywebview.api.get_state();
          if (!appState.browser_ready) {
            setupBrowserView();
            showView('browser');
          } else if (state.firstRun) {
            setupWizardView();
            showView('wizard');
          } else {
            setupDashboardView();
            showView('dashboard');
          }
        } else {
          errBanner.textContent = res.error || 'Activation failed.';
          errBanner.classList.remove('hidden');
        }
      } catch (err) {
        errBanner.textContent = String(err);
        errBanner.classList.remove('hidden');
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Activate';
      }
    };
  }

  // =========================================================================
  // Browser Setup View
  // =========================================================================

  function setupBrowserView() {
    const startBtn = document.getElementById('browser-start');
    const statusEl = document.getElementById('browser-status');
    const errBanner = document.getElementById('browser-error');

    startBtn.onclick = async () => {
      startBtn.disabled = true;
      statusEl.textContent = 'Downloading Chromium. This takes a few minutes.';
      errBanner.classList.add('hidden');

      try {
        const res = await window.pywebview.api.prepare_browser();
        if (res.ok) {
          if (state.firstRun) {
            setupWizardView();
            showView('wizard');
          } else {
            setupDashboardView();
            showView('dashboard');
          }
        } else {
          errBanner.textContent = res.message || 'Browser setup failed.';
          errBanner.classList.remove('hidden');
        }
      } catch (err) {
        errBanner.textContent = String(err);
        errBanner.classList.remove('hidden');
      } finally {
        startBtn.disabled = false;
      }
    };
  }

  // =========================================================================
  // Profile Wizard
  // =========================================================================

  function setupWizardView() {
    let currentStep = 0;
    const totalSteps = 5;
    const stepsOl = document.getElementById('wizard-steps');
    stepsOl.innerHTML = '';
    for (let i = 0; i < totalSteps; i++) {
      const li = document.createElement('li');
      if (i === 0) li.classList.add('active');
      stepsOl.appendChild(li);
    }

    const backBtn = document.getElementById('wizard-back');
    const nextBtn = document.getElementById('wizard-next');
    const scoreSlider = document.getElementById('f-min_match_score');
    const scoreVal = document.getElementById('score-value');
    if (scoreSlider && scoreVal) {
      scoreSlider.oninput = () => { scoreVal.textContent = scoreSlider.value; };
    }

    setupTagInputs('tags-job_titles');
    setupTagInputs('tags-locations');
    setupTagInputs('tags-skills');

    const pickResumeBtn = document.getElementById('pick-resume');
    const resumeNameEl = document.getElementById('resume-name');
    let selectedResumePath = '';
    // Fields the profile stores but the wizard has no input for. save_profile
    // rewrites the whole file, so they have to ride along or they are lost.
    let passthrough = {};

    // Re-hydrate from the saved profile. `get_profile` returns the flat shape
    // that `save_profile` consumes, so every key here round-trips: anything not
    // repopulated would be silently blanked when the wizard saves.
    window.pywebview.api.get_profile().then(p => {
      if (!p) return;
      const profile = p;
      passthrough = { postal_code: profile.postal_code || '' };

      [
        'first_name', 'last_name', 'email', 'phone_country_code', 'phone_number',
        'city', 'state', 'country', 'linkedin_url',
        'current_role', 'current_company', 'professional_summary',
        'total_years_experience', 'max_applications_per_day',
        'work_country', 'notice_period_days',
        'expected_ctc_lakhs', 'minimum_ctc_lakhs',
      ].forEach(key => {
        const val = profile[key];
        if (val !== undefined && val !== null && val !== '') setVal(`f-${key}`, val);
      });

      setTags('tags-job_titles', profile.job_titles || []);
      setTags('tags-locations', profile.locations || []);
      setTags('tags-skills', profile.skills || []);

      if (profile.min_match_score) {
        setVal('f-min_match_score', profile.min_match_score);
        if (scoreVal) scoreVal.textContent = profile.min_match_score;
      }

      setChecked('f-requires_sponsorship', profile.requires_sponsorship, false);
      setChecked('f-willing_to_relocate', profile.willing_to_relocate, true);
      setChecked('f-can_work_remotely', profile.can_work_remotely, true);

      if (profile.resume_path) {
        selectedResumePath = profile.resume_path;
        resumeNameEl.textContent = profile.resume_path.split(/[/\\]/).pop();
      }
    });

    if (pickResumeBtn) {
      pickResumeBtn.onclick = async () => {
        const res = await window.pywebview.api.pick_resume();
        if (res.ok && res.path) {
          selectedResumePath = res.path;
          resumeNameEl.textContent = res.path.split(/[/\\]/).pop();
        }
      };
    }

    function updateStepDisplay() {
      document.querySelectorAll('.step-panel').forEach(p => {
        if (parseInt(p.dataset.step, 10) === currentStep) {
          p.classList.remove('hidden');
        } else {
          p.classList.add('hidden');
        }
      });

      stepsOl.querySelectorAll('li').forEach((li, idx) => {
        li.className = '';
        if (idx < currentStep) li.classList.add('done');
        if (idx === currentStep) li.classList.add('active');
      });

      backBtn.style.visibility = currentStep === 0 ? 'hidden' : 'visible';
      nextBtn.textContent = currentStep === totalSteps - 1 ? 'Save profile' : 'Continue';
    }

    backBtn.onclick = () => {
      if (currentStep > 0) {
        currentStep--;
        updateStepDisplay();
      }
    };

    nextBtn.onclick = async () => {
      if (currentStep < totalSteps - 1) {
        currentStep++;
        updateStepDisplay();
      } else {
        // Collect and save
        const profileData = {
          ...passthrough,
          first_name: getVal('f-first_name'),
          last_name: getVal('f-last_name'),
          email: getVal('f-email'),
          phone_country_code: getVal('f-phone_country_code'),
          phone_number: getVal('f-phone_number'),
          city: getVal('f-city'),
          state: getVal('f-state'),
          country: getVal('f-country'),
          linkedin_url: getVal('f-linkedin_url'),
          job_titles: getTags('tags-job_titles'),
          locations: getTags('tags-locations'),
          min_match_score: parseInt(getVal('f-min_match_score'), 10) || 75,
          max_applications_per_day: parseInt(getVal('f-max_applications_per_day'), 10) || 20,
          total_years_experience: parseInt(getVal('f-total_years_experience'), 10) || 0,
          current_role: getVal('f-current_role'),
          current_company: getVal('f-current_company'),
          skills: getTags('tags-skills'),
          professional_summary: getVal('f-professional_summary'),
          work_country: getVal('f-work_country'),
          requires_sponsorship: getChecked('f-requires_sponsorship', false),
          notice_period_days: parseInt(getVal('f-notice_period_days'), 10) || 30,
          expected_ctc_lakhs: parseFloat(getVal('f-expected_ctc_lakhs')) || 0,
          minimum_ctc_lakhs: parseFloat(getVal('f-minimum_ctc_lakhs')) || 0,
          willing_to_relocate: getChecked('f-willing_to_relocate', true),
          can_work_remotely: getChecked('f-can_work_remotely', true),
          resume_path: selectedResumePath,
        };

        const res = await window.pywebview.api.save_profile(profileData);
        if (res.ok) {
          state.firstRun = false;
          setupDashboardView();
          showView('dashboard');
        } else {
          const errEl = document.getElementById('wizard-error');
          if (errEl) {
            errEl.textContent = res.error || 'Failed to save profile.';
            errEl.classList.remove('hidden');
          }
        }
      }
    };

    updateStepDisplay();
  }

  // =========================================================================
  // Dashboard & Execution Deck
  // =========================================================================

  function setupDashboardView() {
    // Sidebar Nav Handlers
    dom.navItems.forEach(item => {
      item.onclick = () => showTab(item.dataset.tab);
    });

    // License Badge & Paths
    const licenceBadge = document.getElementById('dash-licence-badge');
    if (state.licence && licenceBadge) {
      licenceBadge.textContent = state.licence.key_id || 'Licensed';
    }

    const pathsHint = document.getElementById('settings-paths');
    window.pywebview.api.get_state().then(st => {
      if (pathsHint) pathsHint.textContent = st.data_dir;
    });

    // ---- Pacing & limits -------------------------------------------------
    // Mirrors PACING_FIELDS in gui.py. These settings were always being
    // honoured; until now nothing in the app admitted they existed, so a run
    // that stopped on one looked like a fault rather than a setting.
    const PACE_MAP = {
      'pace-hours-start':  'rhythm.active_hours_start',
      'pace-hours-end':    'rhythm.active_hours_end',
      'pace-session-min':  'rhythm.session_minutes_min',
      'pace-session-max':  'rhythm.session_minutes_max',
      'pace-rest-day':     'rhythm.rest_day_probability',
      'pace-enabled':      'rhythm.enabled',
      'pace-apply-cap':    'limits.max_applications_per_day',
      'pace-min-score':    'search.min_match_score',
      'pace-prefilter':    'matching.prefilter_reject_below',
      'pace-external':     'search.external_apply',
    };
    const paceMsg = document.getElementById('pace-msg');

    function paceNote(text, isError) {
      if (!paceMsg) return;
      paceMsg.textContent = text;
      paceMsg.classList.remove('hidden');
      paceMsg.style.color = isError ? 'var(--danger, #c0392b)' : '';
    }

    function loadPacing() {
      if (!window.pywebview.api.get_pacing) return;
      window.pywebview.api.get_pacing().then(res => {
        if (!res || !res.ok) return;
        Object.entries(PACE_MAP).forEach(([id, dotted]) => {
          const el = document.getElementById(id);
          if (!el || res.values[dotted] === undefined) return;
          el.value = (el.tagName === 'SELECT')
            ? String(res.values[dotted])
            : res.values[dotted];
        });
      });
    }
    loadPacing();

    const paceSave = document.getElementById('pace-save');
    if (paceSave) {
      paceSave.onclick = () => {
        const payload = {};
        Object.entries(PACE_MAP).forEach(([id, dotted]) => {
          const el = document.getElementById(id);
          if (!el || el.value === '') return;
          payload[dotted] = (id === 'pace-enabled') ? (el.value === 'true') : el.value;
        });
        paceSave.disabled = true;
        paceNote('Saving…', false);
        window.pywebview.api.save_pacing(payload).then(res => {
          paceSave.disabled = false;
          if (res && res.ok) {
            paceNote('Saved. Takes effect on the next run.', false);
          } else {
            paceNote((res && res.error) || 'Could not save.', true);
          }
        });
      };
    }

    const paceReimport = document.getElementById('pace-reimport-cv');
    if (paceReimport) {
      paceReimport.onclick = () => {
        paceReimport.disabled = true;
        paceNote('Reading your CV…', false);
        window.pywebview.api.reimport_cv().then(res => {
          paceReimport.disabled = false;
          if (res && res.ok) {
            const i = res.imported || {};
            paceNote(
              `Imported ${i.roles || 0} role(s), ${i.bullets || 0} bullet(s), ` +
              `${i.skills || 0} skill(s), ${i.education || 0} education entr(ies).`, false);
          } else {
            paceNote((res && res.error) || 'Could not read the CV.', true);
          }
        });
      };
    }

    // Mode Segmented Switcher (Dry Run vs Live Apply)
    const btnDry = document.getElementById('mode-btn-dry');
    const btnLive = document.getElementById('mode-btn-live');
    const bannerEl = document.getElementById('mode-banner');
    const btnLinkedin = document.getElementById('btn-run-linkedin');
    const btnNaukri = document.getElementById('btn-run-naukri');

    function updateModeUI() {
      // `is-live` turns the run keys red. Submitting under the operator's name
      // is the one irreversible thing this app does, so the control that does
      // it has to look different from the one that does not.
      dom.views.dashboard.classList.toggle('is-live', state.liveMode);

      if (state.liveMode) {
        btnDry.classList.remove('active');
        btnLive.classList.add('active');
        bannerEl.className = 'mode-info-banner live';
        bannerEl.innerHTML = `
          <span class="mode-banner-icon"><svg class="icon"><use href="#i-signal"/></svg></span>
          <div class="mode-banner-text">
            <strong>Live</strong>
            <span>The agent submits applications under your name. Each one uses a tailored resume held within 10% of your master.</span>
          </div>
        `;
        if (btnLinkedin) btnLinkedin.querySelector('.btn-text').textContent = 'Run LinkedIn — live';
        if (btnNaukri) btnNaukri.querySelector('.btn-text').textContent = 'Run Naukri — live';
      } else {
        btnDry.classList.add('active');
        btnLive.classList.remove('active');
        bannerEl.className = 'mode-info-banner dry';
        bannerEl.innerHTML = `
          <span class="mode-banner-icon"><svg class="icon"><use href="#i-beaker"/></svg></span>
          <div class="mode-banner-text">
            <strong>Dry run</strong>
            <span>The agent searches, scores, tailors a resume, and fills the form, then stops before Submit.</span>
          </div>
        `;
        if (btnLinkedin) btnLinkedin.querySelector('.btn-text').textContent = 'Run LinkedIn — dry run';
        if (btnNaukri) btnNaukri.querySelector('.btn-text').textContent = 'Run Naukri — dry run';
      }
    }

    if (btnDry) btnDry.onclick = () => { state.liveMode = false; updateModeUI(); };
    if (btnLive) btnLive.onclick = () => { state.liveMode = true; updateModeUI(); };

    // Fail closed, then paint. This function also runs when the user returns
    // from editing their profile: without the reset, a liveMode left over from
    // before would survive while the markup reverted to its Dry-Run defaults,
    // and the next click would submit real applications from a UI labelled
    // "Dry-Run". The mode is a per-session choice, so re-arm it deliberately.
    state.liveMode = false;
    updateModeUI();

    // Run & Stop Execution
    const stopBtn = document.getElementById('stop-run');
    const runBtns = document.querySelectorAll('.btn-run');

    runBtns.forEach(b => {
      b.onclick = async () => {
        const platform = b.dataset.platform || 'linkedin';
        // Remembered so arming the schedule uses the platform the user last
        // chose to run, rather than always assuming LinkedIn.
        state.autoPlatform = platform;
        setRunningState(true);
        const res = await window.pywebview.api.start_run(platform, state.liveMode);
        if (!res.ok) {
          appendLog(`[ERROR] ${res.error}`);
          setRunningState(false);
        }
      };
    });

    if (stopBtn) {
      stopBtn.onclick = async () => {
        stopBtn.disabled = true;
        await window.pywebview.api.stop_run();
        stopBtn.disabled = false;
      };
    }

    // Unattended running. The platform and dry/live mode are read at the moment
    // the schedule is armed, so a later flip of the Live switch cannot silently
    // turn a dry schedule into one that submits real applications.
    const autoChk = document.getElementById('chk-auto-run');
    const autoSel = document.getElementById('sel-auto-interval');
    if (autoChk) {
      autoChk.onchange = async () => {
        const on = autoChk.checked;
        if (!on) {
          await window.pywebview.api.set_schedule(false);
          return;
        }
        if (state.liveMode && !confirm(
          'Run automatically in LIVE mode? The agent will submit real applications '
          + 'on this schedule, within its active hours and daily limit, until you turn this off.'
        )) {
          autoChk.checked = false;
          return;
        }
        const minutes = parseInt((autoSel && autoSel.value) || '60', 10);
        const res = await window.pywebview.api.set_schedule(true, state.autoPlatform || 'linkedin', state.liveMode, minutes);
        if (!res.ok) {
          appendLog(`[ERROR] ${res.error}`);
          autoChk.checked = false;
        }
      };
    }

    // Header buttons
    const btnOpenFolder = document.getElementById('btn-open-folder');
    if (btnOpenFolder) {
      btnOpenFolder.onclick = () => window.pywebview.api.open_folder('root');
    }

    // Terminal controls
    const copyLogsBtn = document.getElementById('btn-copy-logs');
    if (copyLogsBtn) {
      // Only the label is swapped -- writing textContent on the button would
      // drop the <svg> icon and never put it back.
      const copyLabel = copyLogsBtn.querySelector('span');
      copyLogsBtn.onclick = () => {
        const logText = document.getElementById('log').textContent;
        navigator.clipboard.writeText(logText).then(() => {
          copyLabel.textContent = 'Copied';
          setTimeout(() => { copyLabel.textContent = 'Copy'; }, 1800);
        });
      };
    }

    const clearLogsBtn = document.getElementById('btn-clear-logs');
    if (clearLogsBtn) {
      clearLogsBtn.onclick = async () => {
        await window.pywebview.api.clear_logs();
        document.getElementById('log').textContent = '';
      };
    }

    const autoScrollChk = document.getElementById('chk-autoscroll');
    if (autoScrollChk) {
      autoScrollChk.onchange = () => { state.autoscroll = autoScrollChk.checked; };
    }

    // Settings tab buttons
    const editProfileBtn = document.getElementById('edit-profile');
    if (editProfileBtn) {
      editProfileBtn.onclick = () => {
        setupWizardView();
        showView('wizard');
      };
    }

    const signOutBtn = document.getElementById('sign-out');
    if (signOutBtn) {
      signOutBtn.onclick = async () => {
        if (confirm('Remove the licence from this machine? You will need the key again to start the agent.')) {
          await window.pywebview.api.sign_out();
          location.reload();
        }
      };
    }

    // Table controls. Bound once here rather than at the tail of the
    // render functions -- those return early on an empty list, which would
    // leave Search and Refresh dead for anyone who has not run the agent yet.
    const searchApps = document.getElementById('input-search-apps');
    if (searchApps) searchApps.oninput = renderApplicationsTable;
    const statusSelect = document.getElementById('select-filter-status');
    if (statusSelect) statusSelect.onchange = renderApplicationsTable;
    const refreshApps = document.getElementById('btn-refresh-apps');
    if (refreshApps) refreshApps.onclick = loadApplications;

    setupBaseResumeControl();

    // Modal close button
    setupModal();

    // Start Polling Loop
    startPolling();
    loadApplications();
  }

  // =========================================================================
  // Settings: base resume
  // =========================================================================

  // Swapping the base resume out of Settings, rather than only inside the
  // wizard, so replacing the document the ±10% audit measures against does not
  // mean walking back through five steps of a profile that is already correct.
  function setupBaseResumeControl() {
    const pickBtn = document.getElementById('settings-pick-resume');
    const nameEl = document.getElementById('settings-resume-name');
    const msgEl = document.getElementById('settings-resume-msg');
    if (!pickBtn || !nameEl) return;

    function showMsg(text, isError) {
      if (!msgEl) return;
      msgEl.textContent = text;
      msgEl.classList.remove('hidden');
      msgEl.classList.toggle('error-banner', Boolean(isError));
    }

    window.pywebview.api.get_profile().then(p => {
      if (p && p.resume_path) nameEl.textContent = p.resume_path.split(/[/\\]/).pop();
    });

    pickBtn.onclick = async () => {
      const picked = await window.pywebview.api.pick_resume();
      if (!picked.ok) {
        if (!picked.cancelled) showMsg(picked.error || 'Could not read that file.', true);
        return;
      }
      pickBtn.disabled = true;
      try {
        // set_base_resume copies the PDF into the app's own resumes folder and
        // rewrites only resume_path, so the rest of the profile is untouched.
        const res = await window.pywebview.api.set_base_resume(picked.path);
        if (res.ok) {
          nameEl.textContent = res.name;
          showMsg('Saved. Resumes generated from now on are measured against this file.', false);
        } else {
          showMsg(res.error || 'Could not save that resume.', true);
        }
      } finally {
        pickBtn.disabled = false;
      }
    };
  }

  // =========================================================================
  // Polling Loop & Live Logs
  // =========================================================================

  function startPolling() {
    if (state.pollInterval) clearInterval(state.pollInterval);

    state.pollInterval = setInterval(async () => {
      try {
        const poll = await window.pywebview.api.poll();
        setRunningState(poll.running);

        if (poll.lines && poll.lines.length > 0) {
          const logEl = document.getElementById('log');
          logEl.textContent = poll.lines.join('\n');
          if (state.autoscroll) {
            logEl.scrollTop = logEl.scrollHeight;
          }
        }

        if (poll.stats) {
          updateStats(poll.stats);
        }

        if (poll.schedule) {
          renderSchedule(poll.schedule);
        }
      } catch (err) {
        console.error('Polling error:', err);
      }
    }, 1000);
  }

  // The scheduler lives in the Python process, not here: a run has to keep
  // going while this page is busy, reloading or hidden, and the interval that
  // matters is measured against the agent's own session clock.
  function renderSchedule(schedule) {
    const chk = document.getElementById('chk-auto-run');
    const sel = document.getElementById('sel-auto-interval');
    const note = document.getElementById('auto-run-note');
    if (!chk || !note) return;

    // Do not fight the user mid-click; the checkbox is only re-synced when it
    // disagrees with the backend, which is the authority on whether the loop
    // is actually alive.
    if (chk.checked !== schedule.enabled) chk.checked = schedule.enabled;
    if (sel && !schedule.enabled) sel.disabled = false;
    if (sel && schedule.enabled) sel.disabled = true;

    if (!schedule.enabled) {
      note.textContent = 'Starts a run on that schedule using the mode and platform selected above. '
        + 'The agent still keeps to its own active hours, breaks and daily limit.';
      return;
    }

    const wait = Math.max(0, Math.round((schedule.next_run * 1000 - Date.now()) / 60000));
    const when = state.running ? 'running now' : (wait <= 0 ? 'starting shortly' : `next run in about ${wait} min`);
    note.textContent = `Automatic ${schedule.platform} runs, ${schedule.live ? 'LIVE' : 'dry run'} — `
      + `${schedule.runs_started} started this session, ${when}. `
      + 'Runs outside the agent’s active hours stop straight away.';
  }

  function setRunningState(running) {
    state.running = running;
    const runBtns = document.querySelectorAll('.btn-run');
    const stopBtn = document.getElementById('stop-run');
    const sidebarPulse = document.getElementById('sidebar-pulse');
    const sidebarText = document.getElementById('sidebar-run-text');
    const deckPulse = document.getElementById('deck-pulse');
    const deckText = document.getElementById('deck-status-text');

    runBtns.forEach(b => { b.disabled = running; });

    if (running) {
      if (stopBtn) stopBtn.classList.remove('hidden');
      if (sidebarPulse) { sidebarPulse.className = 'pulse-dot running'; }
      if (sidebarText) sidebarText.textContent = 'Running';
      if (deckPulse) { deckPulse.className = 'pulse-dot running'; }
      if (deckText) deckText.textContent = 'Running';
    } else {
      if (stopBtn) stopBtn.classList.add('hidden');
      if (sidebarPulse) { sidebarPulse.className = 'pulse-dot idle'; }
      if (sidebarText) sidebarText.textContent = 'Idle';
      if (deckPulse) { deckPulse.className = 'pulse-dot idle'; }
      if (deckText) deckText.textContent = 'Idle';
    }
  }

  function updateStats(stats) {
    const kpiJobs = document.getElementById('kpi-jobs-found');
    const kpiMatches = document.getElementById('kpi-matches');
    const kpiApplied = document.getElementById('kpi-applied-today');
    const kpiOutreach = document.getElementById('kpi-outreach');

    if (kpiJobs) kpiJobs.textContent = numOr(stats.total_jobs_found, 0);
    if (kpiMatches) kpiMatches.textContent = numOr(stats.matches_found, 0);
    if (kpiApplied) kpiApplied.textContent = numOr(stats.applied_jobs, 0);
    if (kpiOutreach) kpiOutreach.textContent = numOr(stats.outreach_sent, 0);

    const badgeApplied = document.getElementById('badge-applied-count');
    if (badgeApplied) badgeApplied.textContent = numOr(stats.applied_jobs, 0);
  }

  function appendLog(line) {
    const logEl = document.getElementById('log');
    logEl.textContent += `\n${line}`;
    if (state.autoscroll) logEl.scrollTop = logEl.scrollHeight;
  }

  // =========================================================================
  // TAB 2: Applications Tracker (Click & View Resumes Used)
  // =========================================================================

  async function loadApplications() {
    const tbody = document.getElementById('apps-table-body');
    if (!tbody) return;

    try {
      const apps = await window.pywebview.api.get_applications();
      state.allApplications = apps;
      renderApplicationsTable();
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty-state">Couldn't load jobs: ${escapeHtml(String(err))}</td></tr>`;
    }
  }

  function renderApplicationsTable() {
    const tbody = document.getElementById('apps-table-body');
    const searchVal = (document.getElementById('input-search-apps')?.value || '').toLowerCase();
    const statusFilter = document.getElementById('select-filter-status')?.value || 'all';

    const filtered = state.allApplications.filter(job => {
      const matchesSearch = !searchVal ||
        (job.title && job.title.toLowerCase().includes(searchVal)) ||
        (job.company && job.company.toLowerCase().includes(searchVal)) ||
        (job.location && job.location.toLowerCase().includes(searchVal));

      const matchesStatus = statusFilter === 'all' || job.status === statusFilter;
      return matchesSearch && matchesStatus;
    });

    if (filtered.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty-state">Nothing here yet. Start a run and jobs will appear as the agent finds them.</td></tr>`;
      return;
    }

    tbody.innerHTML = '';
    filtered.forEach(job => {
      const tr = document.createElement('tr');

      // 1. Title
      const tdTitle = document.createElement('td');
      tdTitle.innerHTML = `<span class="app-title-cell">${escapeHtml(job.title || 'Untitled')}</span>`;

      // 2. Company
      const tdComp = document.createElement('td');
      tdComp.innerHTML = `<span class="app-company-cell">${escapeHtml(job.company || 'Unknown')}</span>`;

      // 3. Location
      const tdLoc = document.createElement('td');
      tdLoc.innerHTML = `<span class="app-location-cell">${escapeHtml(job.location || 'Remote/India')}</span>`;

      // 4. Status
      const tdStatus = document.createElement('td');
      const st = job.status || 'discovered';
      tdStatus.innerHTML = `<span class="badge-status ${st}">${formatStatus(st)}</span>`;

      // 5. Match Score
      const tdMatch = document.createElement('td');
      const score = job.match_score || 0;
      const scoreClass = score >= 75 ? 'high' : (score >= 50 ? 'mid' : 'low');
      tdMatch.innerHTML = `<span class="match-score-pill ${scoreClass}">${score}%</span>`;

      // 6. Resume Used (Click to View!)
      const tdResume = document.createElement('td');
      if (job.resume) {
        const btnView = document.createElement('button');
        btnView.className = 'btn-view-resume';
        btnView.innerHTML = '<svg class="icon icon-sm"><use href="#i-doc"/></svg><span>View</span>';
        btnView.onclick = () => openResumeModal(job.resume, job);
        tdResume.appendChild(btnView);
      } else {
        // No tailored resume exists for this job -- the agent rejected it before
        // reaching the apply step. Say so rather than implying one was used.
        tdResume.innerHTML = `<span class="optional">None</span>`;
      }

      // 7. Actions (Job Link)
      const tdActions = document.createElement('td');
      if (job.url) {
        const btnLink = document.createElement('button');
        btnLink.className = 'btn btn-ghost btn-xs';
        btnLink.innerHTML = '<svg class="icon icon-sm"><use href="#i-link"/></svg><span>Posting</span>';
        btnLink.onclick = () => window.pywebview.api.open_url(job.url);
        tdActions.appendChild(btnLink);
      }

      tr.appendChild(tdTitle);
      tr.appendChild(tdComp);
      tr.appendChild(tdLoc);
      tr.appendChild(tdStatus);
      tr.appendChild(tdMatch);
      tr.appendChild(tdResume);
      tr.appendChild(tdActions);

      tbody.appendChild(tr);
    });
  }

  // =========================================================================
  // Resume Inspection Modal
  // =========================================================================

  function setupModal() {
    const closeBtn = document.getElementById('btn-close-modal');
    const modalCloseBtn = document.getElementById('modal-btn-close');
    const openPdfBtn = document.getElementById('modal-btn-open-pdf');

    if (closeBtn) closeBtn.onclick = () => dom.modal.close();
    if (modalCloseBtn) modalCloseBtn.onclick = () => dom.modal.close();

    if (openPdfBtn) {
      openPdfBtn.onclick = () => {
        if (state.activeModalResume && state.activeModalResume.pdf_path) {
          window.pywebview.api.open_resume(state.activeModalResume.pdf_path);
        }
      };
    }
  }

  function openResumeModal(resume, job = null) {
    state.activeModalResume = resume;

    const titleEl = document.getElementById('modal-resume-title');
    const compEl = document.getElementById('modal-resume-company');
    const badgeType = document.getElementById('modal-badge-type');
    const fidelityEl = document.getElementById('modal-fidelity');
    const deviationEl = document.getElementById('modal-deviation');
    const constraintEl = document.getElementById('modal-constraint');
    const verdictEl = document.getElementById('modal-verdict');
    const findingsEl = document.getElementById('modal-findings');
    const summaryEl = document.getElementById('modal-summary');
    const bulletsEl = document.getElementById('modal-bullets');
    const gcsPathEl = document.getElementById('modal-gcs-path');
    const auditorSection = document.getElementById('modal-section-auditor');

    const roleTitle = resume.target_role || resume.job_title || (job ? job.title : 'Resume');
    const company = resume.company || (job ? job.company : '');

    titleEl.textContent = roleTitle;
    compEl.textContent = company ? `Tailored for ${company}` : 'Master profile';
    badgeType.textContent = resume.is_master ? 'Master resume' : 'Tailored and audited';

    const fidelity = numOr(resume.fidelity_score, 100);
    const deviation = numOr(resume.deviation_percent, 0);
    fidelityEl.textContent = `${fidelity}%`;
    deviationEl.textContent = `${deviation}%`;

    // The audit verdict is the whole point of this panel, so a failure has to
    // read as one. Leaving these green would show a rejected resume as clean.
    const withinLimit = resume.is_within_10_percent_limit !== false;
    constraintEl.textContent = withinLimit ? 'Pass' : 'Fail';
    constraintEl.className = withinLimit ? 'kpi-val green' : 'kpi-val red';

    const verdict = resume.auditor_verdict || 'APPROVED';
    verdictEl.textContent = titleCase(verdict);
    verdictEl.className = verdict === 'APPROVED' ? 'kpi-val green' : 'kpi-val red';
    fidelityEl.className = fidelity >= 85 ? 'kpi-val green' : 'kpi-val amber';

    // Put the needle on the scale. A master resume has no master to deviate
    // from, so the gauge is dropped rather than pinned at a meaningless zero.
    const tol = document.getElementById('modal-tolerance');
    const tolSection = tol ? tol.closest('.modal-section') : null;
    if (tolSection) {
      tolSection.classList.toggle('hidden', Boolean(resume.is_master));
      tol.classList.toggle('out', !withinLimit);
      tol.style.setProperty('--pos', tolPosition(deviation));
    }

    if (resume.auditor_findings) {
      auditorSection.classList.remove('hidden');
      findingsEl.textContent = resume.auditor_findings;
    } else {
      findingsEl.textContent = 'No discrepancies. Dates, employers, titles, and degrees match your master resume exactly.';
    }

    summaryEl.textContent = resume.tailored_summary || 'No tailored summary was recorded for this resume.';

    // Bullets
    bulletsEl.innerHTML = '';
    const allBullets = [
      ...(resume.exp1_bullets || []),
      ...(resume.exp2_bullets || []),
      ...(resume.exp3_bullets || []),
    ];

    if (allBullets.length > 0) {
      allBullets.slice(0, 6).forEach(b => {
        const li = document.createElement('li');
        li.textContent = b;
        bulletsEl.appendChild(li);
      });
    } else {
      bulletsEl.innerHTML = '<li>No experience bullets were recorded for this resume.</li>';
    }

    gcsPathEl.textContent = resume.gcs_pdf_uri || 'Not backed up yet. It is copied to Cloud Storage the next time a run generates it.';

    // A rejected resume is never rendered, so there is no file to open. The
    // button used to stay enabled and silently do nothing when clicked.
    const openPdfBtn = document.getElementById('modal-btn-open-pdf');
    if (openPdfBtn) {
      const hasPdf = Boolean(resume.pdf_path && resume.pdf_exists !== false);
      openPdfBtn.disabled = !hasPdf;
      openPdfBtn.title = hasPdf ? '' : 'No PDF was rendered for this resume.';
    }

    dom.modal.showModal();
  }

  // =========================================================================
  // Tag Inputs & Form Helpers
  // =========================================================================

  function setupTagInputs(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = 'Type and press Enter…';
    container.appendChild(input);

    input.onkeydown = (e) => {
      if (e.key === 'Enter' || e.key === ',') {
        e.preventDefault();
        const val = input.value.trim().replace(/,/g, '');
        if (val) {
          addTag(container, val);
          input.value = '';
        }
      } else if (e.key === 'Backspace' && !input.value) {
        const chips = container.querySelectorAll('.tag-chip');
        if (chips.length > 0) chips[chips.length - 1].remove();
      }
    };
  }

  function addTag(container, text) {
    const chip = document.createElement('span');
    chip.className = 'tag-chip';
    chip.innerHTML = `<span>${escapeHtml(text)}</span><button type="button">×</button>`;
    chip.querySelector('button').onclick = () => chip.remove();
    const input = container.querySelector('input');
    container.insertBefore(chip, input);
  }

  function getTags(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return [];
    const chips = container.querySelectorAll('.tag-chip span');
    return Array.from(chips).map(c => c.textContent.trim()).filter(Boolean);
  }

  function setTags(containerId, tags) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.querySelectorAll('.tag-chip').forEach(c => c.remove());
    (tags || []).forEach(t => addTag(container, t));
  }

  function getVal(id) {
    const el = document.getElementById(id);
    return el ? el.value.trim() : '';
  }

  // `x || fallback` turns a legitimate 0 into the fallback, which would report a
  // 0% deviation resume as the 5% default. Only substitute when there is no number.
  function numOr(val, fallback) {
    return typeof val === 'number' && !Number.isNaN(val) ? val : fallback;
  }

  // Deviation -> a position on the gauge. The scale runs 0-20%, so the ±10%
  // limit sits at dead centre and anything at or past 20% pins to the right
  // edge instead of overflowing the track.
  const TOL_SCALE_MAX = 20;
  function tolPosition(deviation) {
    const d = Math.max(0, Math.min(numOr(deviation, 0), TOL_SCALE_MAX));
    return `${(d / TOL_SCALE_MAX) * 100}%`;
  }

  // Auditor verdicts arrive SHOUTING from the model. Sentence case reads as
  // a status rather than an alarm; the colour already carries the severity.
  function titleCase(str) {
    const s = String(str || '');
    return s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();
  }

  function setVal(id, val) {
    const el = document.getElementById(id);
    if (el) el.value = val;
  }

  // `?.checked || fallback` collapses an intentional false into the fallback,
  // so a cleared checkbox could never be saved. Read the element explicitly.
  function getChecked(id, fallback) {
    const el = document.getElementById(id);
    return el ? el.checked : fallback;
  }

  function setChecked(id, val, fallback) {
    const el = document.getElementById(id);
    if (el) el.checked = (val === undefined || val === null) ? fallback : Boolean(val);
  }

  // Keys are JobStatus values from src/core/models.py. Anything not listed
  // falls through to the raw value rather than rendering blank.
  function formatStatus(status) {
    const map = {
      applied: 'Applied',
      evaluated_match: 'Matched',
      evaluated_reject: 'Rejected',
      discovered: 'Discovered',
      skipped: 'Skipped',
      failed: 'Needs Review',
    };
    return map[status] || status;
  }

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = (str === undefined || str === null) ? '' : String(str);
    return div.innerHTML;
  }

  // Start initialization
  waitForApi(init);
})();

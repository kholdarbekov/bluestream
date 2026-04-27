(function () {
    var PAGE_DATA = getPageData();

    document.addEventListener('DOMContentLoaded', function () {
        var frequencyOptions = document.querySelectorAll('.frequency-option');
        var previewFrequency = document.getElementById('preview-frequency');
        var previewSavings = document.getElementById('preview-savings');
        var selectedFrequency = null;

        function updatePreview() {
            if (!selectedFrequency) return;

            previewFrequency.textContent = PAGE_DATA.frequency_labels[selectedFrequency];

            var discounts = { daily: 5, weekly: 10, biweekly: 15, monthly: 20 };
            var discount = discounts[selectedFrequency] || 0;
            previewSavings.textContent = discount + '%';
        }

        frequencyOptions.forEach(function (option) {
            option.addEventListener('click', function () {
                frequencyOptions.forEach(function (opt) { opt.classList.remove('selected'); });
                this.classList.add('selected');
                selectedFrequency = this.dataset.frequency;
                updatePreview();
            });
        });

        document.querySelectorAll('a[href^="#"]').forEach(function (anchor) {
            anchor.addEventListener('click', function (e) {
                e.preventDefault();
                var href = this.getAttribute('href');
                if (href === '#') return;

                var target = document.querySelector(href);
                if (target) {
                    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
                }
            });
        });

        var accordionButtons = document.querySelectorAll('.faq-section .acc-btn');
        accordionButtons.forEach(function (btn) {
            btn.addEventListener('click', function (e) {
                e.preventDefault();

                var accordionBlock = this.closest('.accordion.block');
                var content = accordionBlock.querySelector('.acc-content');
                var isActive = this.classList.contains('active');

                document.querySelectorAll('.faq-section .accordion.block').forEach(function (block) {
                    block.classList.remove('active-block');
                    block.querySelector('.acc-btn').classList.remove('active');
                    block.querySelector('.acc-content').classList.remove('current');
                });

                if (!isActive) {
                    accordionBlock.classList.add('active-block');
                    this.classList.add('active');
                    content.classList.add('current');
                }
            });
        });

        var observerOptions = {
            threshold: 0.1,
            rootMargin: '0px 0px -100px 0px'
        };

        var observer = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                if (entry.isIntersecting) {
                    entry.target.style.opacity = '1';
                    entry.target.style.transform = 'translateY(0)';
                }
            });
        }, observerOptions);

        document.querySelectorAll('.step-item, .feature-card, .use-case-card').forEach(function (el) {
            el.style.opacity = '0';
            el.style.transform = 'translateY(30px)';
            el.style.transition = 'opacity 0.6s ease, transform 0.6s ease';
            observer.observe(el);
        });
    });
})();

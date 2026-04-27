(function () {
    var PAGE_DATA = getPageData();

    function formatNumber(num) {
        return num.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
    }

    function parseFormattedNumber(str) {
        if (!str) return 0;
        return parseInt(str.toString().replace(/\s/g, ''), 10) || 0;
    }

    function sortProducts(sortBy) {
        var url = new URL(window.location.href);
        url.searchParams.set('sort', sortBy);
        url.searchParams.set('page', '1');
        window.location.href = url.toString();
    }

    $(function () {
        var minPrice = PAGE_DATA.min_price || 0;
        var maxPrice = PAGE_DATA.max_price || 10000000;

        setTimeout(function () {
            if ($('.price-range-slider').length) {
                $('.price-range-slider').slider('destroy');
                $('.price-range-slider').slider({
                    range: true,
                    min: minPrice,
                    max: maxPrice,
                    values: [minPrice, maxPrice],
                    slide: function (event, ui) {
                        $('#price-min').val(formatNumber(ui.values[0]));
                        $('#price-max').val(formatNumber(ui.values[1]));
                    },
                    change: function (event, ui) {
                        $('#price-min').val(formatNumber(ui.values[0]));
                        $('#price-max').val(formatNumber(ui.values[1]));
                    }
                });

                $('#price-min').val(formatNumber($('.price-range-slider').slider('values', 0)));
                $('#price-max').val(formatNumber($('.price-range-slider').slider('values', 1)));
            }
        }, 100);

        $('#price-min').on('input', function () {
            var minVal = parseFormattedNumber($(this).val());
            var maxVal = parseFormattedNumber($('#price-max').val());

            if (minVal > maxVal) minVal = maxVal;
            if (minVal < minPrice) minVal = minPrice;
            if (minVal > maxPrice) minVal = maxPrice;

            $('.price-range-slider').slider('values', [minVal, maxVal]);
        });

        $('#price-max').on('input', function () {
            var minVal = parseFormattedNumber($('#price-min').val());
            var maxVal = parseFormattedNumber($(this).val());

            if (maxVal < minVal) maxVal = minVal;
            if (maxVal < minPrice) maxVal = minPrice;
            if (maxVal > maxPrice) maxVal = maxPrice;

            $('.price-range-slider').slider('values', [minVal, maxVal]);
        });

        $('.price-input').on('blur', function () {
            var val = parseFormattedNumber($(this).val());
            $(this).val(val >= 0 ? formatNumber(val) : '0');
        });

        $('.price-input').on('input', function () {
            var input = this;
            var cursorPos = input.selectionStart;
            var oldLength = input.value.length;

            var cleanValue = input.value.replace(/\D/g, '');

            if (cleanValue) {
                var numValue = parseInt(cleanValue, 10);
                if (!isNaN(numValue) && numValue <= maxPrice) {
                    var formatted = formatNumber(numValue);
                    input.value = formatted;

                    var newLength = formatted.length;
                    var diff = newLength - oldLength;
                    input.setSelectionRange(cursorPos + diff, cursorPos + diff);
                }
            }
        });

        $('.price-input').on('keypress', function (e) {
            var char = String.fromCharCode(e.which);
            if (!/[\d]/.test(char) && e.which !== 8 && e.which !== 0) {
                e.preventDefault();
            }
        });
    });

    $(document).ready(function () {
        setTimeout(function () {
            if ($('.selectmenu').length) {
                $('.selectmenu').niceSelect('destroy');
                $('.selectmenu').niceSelect();
            }
        }, 150);

        var sortSelect = document.querySelector('.selectmenu[data-action="sort-products"]');
        if (sortSelect) {
            sortSelect.addEventListener('change', function () {
                sortProducts(this.value);
            });
        }

        document.body.addEventListener('click', function (e) {
            var btn = e.target.closest('[data-action="add-to-cart"]');
            if (btn && typeof window.addToCart === 'function') {
                window.addToCart(parseInt(btn.dataset.productId, 10));
            }
        });
    });
})();

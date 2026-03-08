import main


def test_finalize_plain_text_outputs_without_correction(tmp_path):
    segments = [(0.0, 1.0, '第一句'), (1.0, 2.0, '第二句')]

    raw_text, display_text, file_names = main._finalize_plain_text_outputs(
        tmp_path,
        'sample',
        segments,
        '第一句\n第二句\n',
    )

    assert raw_text == '第一句\n第二句\n'
    assert display_text == '第一句\n第二句\n'
    assert file_names == ['sample.txt']
    assert (tmp_path / 'sample.txt').read_text(encoding='utf-8') == '第一句\n第二句\n'
    assert not (tmp_path / 'sample.ori.txt').exists()
    assert not (tmp_path / 'sample.矫正.txt').exists()


def test_final_output_filter_accepts_plain_text_and_translation_outputs():
    assert main._is_final_output_file('sample.txt', 'sample')
    assert main._is_final_output_file('sample.zh.txt', 'sample')
    assert not main._is_final_output_file('sample.orig.txt', 'sample')
    assert not main._is_final_output_file('sample.ori.txt', 'sample')
    assert not main._is_final_output_file('sample.矫正.txt', 'sample')
import pytest

from app.quick_reply_locations import coordinates_from_maps_url, normalize_location


@pytest.mark.parametrize('link,expected', [
    ('33.89, 35.50', (33.89, 35.5)),
    ('https://www.google.com/maps/search/?api=1&query=-33.8567%2C151.2152', (-33.8567,151.2152)),
    ('https://maps.google.com/?q=0,0', (0,0)),
    ('https://www.google.com/maps/place/Office/@10,20,12z/data=!3d33.89!4d35.50', (33.89,35.5)),
])
def test_maps_pin_coordinates(link, expected):
    assert coordinates_from_maps_url(link) == expected


@pytest.mark.parametrize('link', [
    'https://maps.app.goo.gl/short', 'https://goo.gl/maps/short',
    'https://www.google.com/maps/@33.89,35.50,13z',
    'https://www.google.com/maps?q=Office',
    'https://www.google.com/maps?q=1,2&q=3,4',
    'https://www.google.com/maps/search/?query=1,2&query_place_id=other-place',
    'https://www.google.com/maps/dir/First/Second/data=!3d1!4d2!3d3!4d4',
    'https://evil.test/maps?q=1,2', 'http://127.0.0.1/maps?q=1,2',
    'https://google.com.evil.test/maps?q=1,2', 'https://user:secret@google.com/maps?q=1,2',
])
def test_unresolvable_or_non_pin_links_are_not_guessed(link):
    with pytest.raises(ValueError):
        coordinates_from_maps_url(link)


def test_location_validation_and_zero_coordinates():
    assert normalize_location({'latitude': 0, 'longitude': 0}, 'Origin') == {
        'latitude': 0, 'longitude': 0, 'name': 'Origin', 'address': ''}
    assert normalize_location({'maps_url': '-90,180', 'address':' Road '}, 'South')['address'] == 'Road'
    for pin, title in [
        ({'latitude': 91, 'longitude': 0}, 'Bad'),
        ({'latitude': 0, 'longitude': -181}, 'Bad'),
        ({'latitude': float('nan'), 'longitude': 0}, 'Bad'),
        ({'latitude': 0, 'longitude': float('inf')}, 'Bad'),
        ({'latitude': 0}, 'Bad'), ({'latitude': 0, 'longitude': 0}, ''),
        ({'maps_url': '1,2', 'latitude': 1}, 'Conflicting'),
        ({'latitude': 0, 'longitude': 0, 'address': 'x'*1001}, 'Long'),
        ({'latitude': 0, 'longitude': 0, 'name': 'bad\x00name'}, 'Bad'),
    ]:
        with pytest.raises(ValueError):
            normalize_location(pin, title)

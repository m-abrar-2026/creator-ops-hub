import os
import pytest
from server.app import create_app
from server.models import db, User, AffiliateLink, Sponsorship


@pytest.fixture
def app():
    os.environ['DB_TYPE'] = 'sqlite'
    app = create_app()
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"
    })

    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def test_signup_and_login(client):
    # Test user signup
    response = client.post('/signup', data={
        'username': 'testcreator',
        'email': 'test@creator.com',
        'password': 'password123'
    }, follow_redirects=True)
    assert response.status_code == 200

    # Test login with correct credentials
    response = client.post('/login', data={
        'username': 'testcreator',
        'password': 'password123'
    }, follow_redirects=True)
    assert response.status_code == 200


def test_affiliate_link_crud(client):
    # Signup and log in first
    client.post('/signup', data={
        'username': 'linkcreator',
        'email': 'link@creator.com',
        'password': 'password123'
    }, follow_redirects=True)

    # Create link
    response = client.post('/links', data={
        'title': 'Test Merch',
        'slug': 'test-merch',
        'destination_url': 'https://example.com/merch'
    }, follow_redirects=True)
    assert response.status_code == 200

    # Verify link exists in DB context
    with client.application.app_context():
        link = AffiliateLink.query.filter_by(slug='test-merch').first()
        assert link is not None
        assert link.title == 'Test Merch'


def test_redirect_and_click_counter(client):
    with client.application.app_context():
        user = User(username='clicktest', email='click@test.com', password_hash='hash')
        db.session.add(user)
        db.session.commit()

        link = AffiliateLink(title='Click Link', slug='click-slug', destination_url='https://example.com',
                             user_id=user.id)
        db.session.add(link)
        db.session.commit()
        link_id = link.id

    # Test the redirect route
    response = client.get('/r/click-slug', follow_redirects=False)
    assert response.status_code == 302
    assert response.headers['Location'] == 'https://example.com'

    # Verify click count incremented
    with client.application.app_context():
        updated_link = db.session.get(AffiliateLink, link_id)
        assert updated_link.click_count == 1